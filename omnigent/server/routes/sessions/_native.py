"""Routes for the Sessions API (``/v1/sessions``).

These endpoints expose a thin, harness-agnostic surface over an
agent's conversation: create a session bound to an agent, post events
(messages, tool outputs, interrupts), read a snapshot, and live-tail
the SSE stream. The session is implemented on top of the existing
conversation-item + task + live-stream machinery — this module is a
boundary translation layer, not a new runtime.

Input dispatch (POST /events) persists the item to
``conversation_items`` and forwards to the bound runner over the WS
tunnel. The persist-before-forward order is invariant I1 in
``designs/SESSION_REARCHITECTURE.md`` — a snapshot read immediately
after POST observes the input in ``items``.

The reconnect contract is **snapshot + live tail**, not replay: a
client opens the live stream and ``GET``s the snapshot, then
deduplicates by item id any events that fire between the two reads.
See ``server/API.md`` for the full contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

import cachetools
import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.datastructures import UploadFile as StarletteUploadFile

from omnigent.blueprints import (
    BlueprintRunner,
    ChildDispatchResult,
    blueprint_events_to_run,
    render_blueprint_value,
)
from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.cost_plan import (
    COST_CONTROL_LABEL_NAMESPACE,
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_agent_id, generate_task_id
from omnigent.entities import (
    Agent,
    BlueprintEventData,
    CommentsFingerprint,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    NON_CONTENT_ITEM_TYPES,
    FunctionCallData,
    FunctionCallOutputData,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.fabric.runner_fabric import (
    FabricRunnerConflict,
    HostRunnerAcquisition,
    HostWorkerRunnerFabric,
)
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.model_override import validate_model_override
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    NativeCodingAgent,
    native_coding_agent_for_agent_name,
    native_coding_agent_for_harness,
    native_coding_agent_for_terminal_name,
    native_coding_agent_for_wrapper_label,
)
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyAction,
    PolicyResult,
)
from omnigent.reasoning_effort import (
    EFFORT_CLEAR_VALUES,
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    event_hub,
    get_agent_cache,
    get_caps,
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
    session_stream,
    user_session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import (
    _ELICITATION_MODE,
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.builder import build_policy_engine, load_session_usage
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server import presence
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.agent_refs import require_agent_ref, resolve_agent_ref
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.conversation_event_sequencer import (
    SequencedPersistResult,
    flush_orphaned_outputs,
    persist_sequenced_item,
)
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
)
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_permission_level as _get_permission_level,
)
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id as _get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._codex_elicitation import parse_codex_elicitation_request
from omnigent.server.routes._content_type import (
    require_json_content_type,
    require_json_or_multipart_content_type,
)
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.runner_heal_config import RunnerHealConfig, load_runner_heal_config
from omnigent.server.schemas import (
    AgentObject,
    BlueprintRunResponse,
    ChildSessionSummary,
    ConversationDeleted,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    ErrorEvent,
    GrantPermissionRequest,
    MCPServerSummary,
    ModelUsage,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    PaginatedList,
    PermissionObject,
    PolicySummary,
    SandboxStatus,
    ServerStreamEvent,
    SessionAgentChangedEvent,
    SessionCreatedEvent,
    SessionCreateMetadata,
    SessionCreateRequest,
    SessionEventInput,
    SessionForkRequest,
    SessionGitOptions,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionLabelsResponse,
    SessionListItem,
    SessionModelEvent,
    SessionResourceListPage,
    SessionResourceObject,
    SessionResourcePaginatedList,
    SessionResponse,
    SessionSandboxStatusEvent,
    SessionSkillsEvent,
    SessionStatusEvent,
    SessionSwitchAgentRequest,
    SessionTerminalPendingEvent,
    SessionTodosEvent,
    SessionUsageEvent,
    SkillSummary,
    UpdateSessionRequest,
)
from omnigent.server.subject_token_stash import (
    evict_subject_token,
    stash_subject_token_from_headers,
)
from omnigent.session_lifecycle import (
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)
from omnigent.spec.types import (
    AgentSpec,
    BlueprintNode,
    FunctionPolicySpec,
    Phase,
    PolicySpec,
    StateUpdate,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import (
    ConversationNotFoundError,
    NameAlreadyExistsError,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.tools.client_specified import parse_client_side_tool_specs

_logger = logging.getLogger(__name__)
from ._constants import *
from ._state import *

_CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "claude-code-native-ui-subagent"

_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY = "omnigent.claude_native.subagent_id"

_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY = "omnigent.claude_native.tool_use_id"

_CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY = "omnigent.claude_native.description"

_CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "codex-native-ui-subagent"

_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.subagent_thread_id"

_CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.parent_thread_id"

_CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY = "omnigent.codex_native.collab_tool_call_id"

_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY = "omnigent.codex_native.prompt"

_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname"

_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY = "omnigent.codex_native.agent_role"

_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK = "Codex"

_CLAUDE_NATIVE_WRAPPER_LABEL_KEY = "omnigent.wrapper"

_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE = CLAUDE_NATIVE_CODING_AGENT.wrapper_label

_CLAUDE_NATIVE_UI_LABEL_KEY = "omnigent.ui"

_CLAUDE_NATIVE_UI_LABEL_VALUE = "terminal"

_CLAUDE_NATIVE_HARNESS = CLAUDE_NATIVE_CODING_AGENT.harness

_CLAUDE_NATIVE_MODEL = CLAUDE_NATIVE_CODING_AGENT.agent_name

_CODEX_NATIVE_WRAPPER_LABEL_VALUE = CODEX_NATIVE_CODING_AGENT.wrapper_label

_CODEX_NATIVE_HARNESS = CODEX_NATIVE_CODING_AGENT.harness

_CODEX_NATIVE_MODEL = CODEX_NATIVE_CODING_AGENT.agent_name

_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S = 30.0

_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"

_NATIVE_TERMINAL_ENSURE_FAILED_CODE = "native_terminal_ensure_failed"

_NATIVE_TERMINAL_ENSURE_TIMEOUT_S = 30.0

_NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS = 4

_NATIVE_TERMINAL_ENSURE_RETRY_DELAY_S = 2.0

_NATIVE_POLICY_NOT_ENFORCED_CODE = "native_policy_not_enforced"

_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0

class _MirroredToolCall:
    """
    Tool identity of a forwarder-mirrored ``function_call``.

    Cached by ``call_id`` so a later ``function_call_output`` (which
    carries only ``call_id`` + ``output``) can recover the tool it
    belongs to and correlate it to a parked permission prompt. See
    :data:`_recent_mirrored_tool_calls`.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param tool_input: Parsed tool arguments, e.g.
        ``{"command": "ls"}``; ``{}`` when the arguments were absent or
        not a JSON object.
    :param response_id: Response id the function call was mirrored
        under. Matching outputs inherit this id so the transcript keeps
        tool calls and results in one rendered response even when the
        forwarder observes the output after a later status edge.
    """

    tool_name: str
    tool_input: dict[str, Any]
    response_id: str

def _persist_native_cumulative_usage(
    session_id: str,
    data: dict[str, Any],
    conversation_store: ConversationStore,
) -> float | None:
    """
    Persist cumulative cost / token usage reported by a native harness.

    Unlike the Omnigent relay path (:func:`_accumulate_session_usage`), which adds
    per-response *deltas*, native harnesses (claude-native / codex-native)
    report *cumulative* session usage — so this writes with SET semantics, not
    add. The two paths never run for the same session, so they don't conflict.

    Reads explicit cumulative fields from the ``external_session_usage`` event's
    ``data`` (all optional; a no-op when none are present):

    - ``cumulative_cost_usd`` — total session cost for DISPLAY, e.g.
      claude-native forwards Claude Code's own ``cost.total_cost_usd``
      (exact billing; used directly). Stored in ``total_cost_usd``, which
      drives the badge and the per-user daily rollup, so the badge matches
      ``/cost`` in the Claude TUI.
    - ``policy_cost_usd`` — total session cost for ENFORCEMENT (the
      cost-budget gate). claude-native forwards ``max(S, real-time
      transcript estimate)`` here so the gate reflects in-flight sub-agent
      spend while the displayed ``S`` is frozen for the sub-agent's run.
      Stored verbatim in ``policy_cost_usd`` (the policy engine seeds from
      it, falling back to ``total_cost_usd`` when absent). Not fed into the
      daily rollup — that uses the authoritative ``total_cost_usd``.
    - ``cumulative_input_tokens`` / ``cumulative_output_tokens`` — total session
      tokens, e.g. codex-native's ``tokenUsage.total``. When
      ``cumulative_cost_usd`` is absent, cost is computed from these via
      :func:`fetch_model_pricing`.
    - ``cumulative_cache_read_input_tokens`` — the cached portion *included
      in* ``cumulative_input_tokens`` (e.g. codex-native's
      ``tokenUsage.total.cachedInputTokens``). Split out of the input total
      so :func:`compute_llm_cost` prices it at the cache-read rate rather
      than the full input rate. Absent for harnesses that don't report it.
    - ``model`` — LLM model id to price with (e.g. ``"databricks-gpt-5-5"``);
      falls back to the agent spec's model when absent.

    The ``total_cost_usd`` key is written only on the priced branches
    below (exact billing, or token-priced when the model is in the
    catalog), so an unpriced native session leaves it absent — the same
    "priced ⟺ key present" contract the relay path uses. ``policy_cost_usd``
    is written only when the event carries it (claude-native with the
    display/policy split); codex-native and the relay omit it and the
    policy engine falls back to ``total_cost_usd``.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param data: The ``external_session_usage`` event ``data`` dict.
    :param conversation_store: Store for reading and writing ``session_usage``.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or no
        cumulative field was present.
    :raises OmnigentError: When a cumulative field is the wrong type.
    """
    cost = _coerce_cumulative_field(data, "cumulative_cost_usd", numeric=True)
    policy_cost = _coerce_cumulative_field(data, "policy_cost_usd", numeric=True)
    cin = _coerce_cumulative_field(data, "cumulative_input_tokens", numeric=False)
    cout = _coerce_cumulative_field(data, "cumulative_output_tokens", numeric=False)
    ccache = _coerce_cumulative_field(data, "cumulative_cache_read_input_tokens", numeric=False)
    if cost is None and policy_cost is None and cin is None and cout is None:
        return None

    conv = conversation_store.get_conversation(session_id)
    current = dict(conv.session_usage) if conv and conv.session_usage else {}
    # Native usage is cumulative (SET semantics), so the per-turn delta
    # for the daily rollup is new_total - old_total. Capture the old
    # cumulative cost before the fields below overwrite it.
    old_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    if cin is not None:
        # The reported input total is INCLUSIVE of cached tokens (codex's
        # ``inputTokens`` counts cache reads). Split the cached portion into
        # its own bucket so compute_llm_cost prices it at the cache-read rate;
        # ``input_tokens`` keeps only the non-cached remainder (its contract).
        # Clamp cached to the total so a malformed report never makes
        # ``input_tokens`` negative.
        cached = min(int(ccache), int(cin)) if ccache is not None else 0
        current["cache_read_input_tokens"] = cached
        current["input_tokens"] = int(cin) - cached
    if cout is not None:
        current["output_tokens"] = cout
    if cin is not None or cout is not None:
        # ``total_tokens`` reflects the full input (non-cached + cached) plus
        # output, so the split above doesn't shrink the displayed total.
        current["total_tokens"] = (
            int(current.get("input_tokens", 0))
            + int(current.get("cache_read_input_tokens", 0))
            + int(current.get("output_tokens", 0))
        )

    # Resolve the model only when tokens are present — both the token-pricing
    # branch and the per-model attribution below need it, and both are gated on
    # tokens. Resolving lazily avoids calling ``_resolve_llm_model`` (which
    # touches the runtime agent cache) on a cost-only broadcast. Computed once
    # out of the pricing-only branch so attribution works even on an unpriced
    # turn. The raw harness model id wins; falls back to the agent spec's model.
    has_tokens = cin is not None or cout is not None
    model_name = (data.get("model") or _resolve_llm_model(conv)) if has_tokens else None
    if cost is not None:
        current["total_cost_usd"] = float(cost)
    elif has_tokens:
        if isinstance(model_name, str) and model_name:
            from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

            pricing = fetch_model_pricing(model_name)
            if pricing is not None:
                # SET (cumulative) — price the running token totals.
                # ``current`` carries the cache-read split when the harness
                # reports it (codex-native does), so compute_llm_cost prices
                # cache reads at their own rate; it falls back to the input
                # rate for cache tokens when the catalog omits a cache price
                # (e.g. ``databricks-*`` entries today).
                current["total_cost_usd"] = compute_llm_cost(current, pricing)

    # Per-model attribution (SET). Native harnesses report cumulative SESSION
    # totals, not per-model splits, so attribute the running cumulative buckets
    # to the current model. For the usual single-model native session this
    # makes the per-model view equal the flat totals; on a mid-session model
    # switch the current model absorbs the cumulative (splitting deferred —
    # keyed on the raw harness model id). Cost mirrors the flat
    # ``total_cost_usd`` so the per-model cost key is present iff priced.
    # ``model_name`` is only set when tokens are present, so this is skipped
    # for cost-only broadcasts (nothing to attribute per-model).
    if isinstance(model_name, str) and model_name:
        bucket = _model_usage_bucket(current, model_name)
        for key in _MODEL_TOKEN_KEYS:
            if key in current:
                bucket[key] = current[key]
        if "total_cost_usd" in current:
            bucket["total_cost_usd"] = current["total_cost_usd"]

    # Enforcement value (claude-native display/policy split). Stored
    # separately from the displayed ``total_cost_usd`` so the gate can read
    # the real-time figure (incl. in-flight sub-agent spend) while the badge
    # shows the frozen statusLine total. SET semantics, like the rest.
    if policy_cost is not None:
        current["policy_cost_usd"] = float(policy_cost)

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup. Native reports cumulative totals, so the turn's
    # delta is the increase in cumulative cost. Uses the authoritative
    # ``total_cost_usd`` (= statusLine S), NOT ``policy_cost_usd`` — the
    # daily report must reflect real spend, not the real-time gate estimate.
    new_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    _record_daily_cost(conv, new_cost - old_cost, conversation_store)
    return _priced_cost_for_display(current)

def _spawn_native_approval_popup_forward(
    session_id: str, elicitation_id: str, message: str, policy_name: str | None = None
) -> None:
    """
    Ask the bound runner to pop a native-terminal modal for a parked ASK.

    Fire-and-forget. Forwards the same ``cost_approval_popup`` control event
    the cost gate uses — the runner dispatch + popup launcher are
    policy-agnostic — so a user working in the native terminal can answer a
    parked tool-policy ASK there, not only in the web ApprovalCard. (Native
    tool-policy ASKs were moved server-side, which took them out of the
    TUI; this puts them back.) The popup resolves the SAME elicitation via
    the same resolve endpoint the web card uses, so whichever surface
    answers first releases the gate. Non-native harnesses 204 no-op on the
    runner.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: The parked elicitation's id, e.g. ``"elicit_x"``.
    :param message: The approval reason shown in the popup.
    :param policy_name: Name of the deciding policy, rendered as the
        popup header so it reflects the actual policy rather than a
        hardcoded cost-budget label. ``None`` falls back to a generic
        header on the runner.
    :returns: None. Fire-and-forget: forwarding failures (runner offline,
        no runner bound) are swallowed by ``_forward_session_change_to_runner``
        and never block the gate — the web ApprovalCard remains the surface.
    """

    async def _forward() -> None:
        await _forward_session_change_to_runner(
            session_id,
            _server_runner_router,
            {
                "type": "cost_approval_popup",
                "elicitation_id": elicitation_id,
                "message": message,
                "policy_name": policy_name,
            },
        )

    task = asyncio.create_task(_forward())
    _native_popup_forward_tasks.add(task)
    task.add_done_callback(_native_popup_forward_tasks.discard)

def _find_claude_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    subagent_id: str,
) -> Conversation | None:
    """
    Look up an existing claude-native sub-agent child by its Claude-
    side ``subagent_id``.

    Used to make :func:`_persist_external_subagent_start` idempotent:
    the forwarder retries on transient HTTP errors, so two POSTs may
    carry the same ``subagent_id`` for the same physical sub-agent —
    we want both to resolve to the same child Conversation row.

    :param conversation_store: Store to query.
    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param subagent_id: Stable Claude-side identifier read from
        ``agent-<id>.meta.json``'s directory name, e.g.
        ``"a5c7effac5a9a35ab"``.
    :returns: The matching child :class:`Conversation`, or ``None``
        when no row has been minted for this sub-agent yet.
    """
    # Page through all children so the lookup isn't capped by result
    # ordering. A parent with > 100 sub-agents would otherwise miss the
    # existing row for an older ``subagent_id`` and fall through to
    # ``create_conversation``, which then trips the
    # ``(parent, title)`` unique constraint instead of returning the
    # existing child id.
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY) == subagent_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id

def _find_codex_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    thread_id: str,
) -> Conversation | None:
    """
    Look up an existing Codex-native sub-agent child by its Codex thread id.

    Makes ``_persist_external_codex_subagent_start`` idempotent: when the
    forwarder re-posts because it observed both ``item/started`` and
    ``item/completed`` for the same collab item, the second POST returns
    the existing child row rather than creating a duplicate.

    :param conversation_store: Store to query.
    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param thread_id: Codex child thread id, e.g.
        ``"019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row exists for this thread id.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY) == thread_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id

def _codex_subagent_display_tool(labels: dict[str, str]) -> str:
    """
    Return the UI-facing label for a Codex child session.

    Uses the Codex-assigned nickname when available, then the agent
    role, then ``"Codex"`` as a generic fallback.

    :param labels: Conversation labels from a Codex child row.
    :returns: Display label, e.g. ``"auth-auditor"``.
    """
    nickname = labels.get(_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY)
    if nickname:
        return nickname
    role = labels.get(_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY)
    if role:
        return role
    return _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK

def _is_codex_native_subagent(conv: Conversation) -> bool:
    """
    Return whether a child conversation tracks a Codex internal sub-agent.

    :param conv: Conversation row to inspect.
    :returns: ``True`` when the row carries the codex-native sub-agent
        wrapper label.
    """
    return (
        conv.kind == "sub_agent"
        and conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        == _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
    )

def _codex_subagent_labels_from_body(
    thread_id: str,
    body: SessionEventInput,
) -> dict[str, str]:
    """
    Build the label dict for a Codex-native sub-agent child row.

    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param body: Validated ``external_codex_subagent_start`` event body.
    :returns: Labels to upsert on the child conversation row.
    """
    labels: dict[str, str] = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: thread_id,
    }
    for data_key, label_key in (
        ("parent_thread_id", _CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY),
        ("tool_call_id", _CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY),
        ("prompt", _CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY),
        ("agent_nickname", _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY),
        ("agent_role", _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY),
    ):
        value = body.data.get(data_key)
        if isinstance(value, str) and value:
            labels[label_key] = value
    return labels

async def _create_and_publish_codex_child(
    parent_id: str,
    parent_conv: Conversation,
    thread_id: str,
    labels: dict[str, str],
    conversation_store: ConversationStore,
) -> str:
    """
    Create a new Codex child Conversation row and publish ``session.created``.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Parent row whose ``agent_id`` and ``runner_id``
        are inherited by the child.
    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param labels: Labels to stamp on the new child row.
    :param conversation_store: Store used to create the child row.
    :returns: New child conversation id, e.g. ``"conv_child456"``.
    """
    # Stable title so the (parent, title) unique index prevents race-condition
    # duplicate rows when the forwarder retries a failed registration.
    title = f"codex-native-ui-subagent:{thread_id}"
    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK,
        )
    except NameAlreadyExistsError:
        # A concurrent POST (or a retry that arrived before set_labels ran)
        # already created the row — find it and upsert labels instead.
        existing = await asyncio.to_thread(
            _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
        )
        if existing is None:
            # The thread-id label never landed (the original POST died
            # between create_conversation and set_labels), so the label
            # lookup can't see the row. The title embeds the same thread
            # id and must exist for the unique index to have fired — fall
            # back to it so redelivery heals the unlabeled row instead of
            # permanently 500ing.
            existing = await asyncio.to_thread(
                _find_subagent_child_by_title,
                conversation_store,
                parent_id,
                title,
            )
        if existing is not None:
            await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
            # An orphaned row's creator died before publishing
            # ``session.created``, so live clients have never heard about
            # this child — emit it now. In the concurrent-race case the
            # winner also published; the duplicate is a harmless extra
            # cache invalidation.
            _publish_session_created(parent_id, existing.id, parent_conv.agent_id)
            return existing.id
        raise
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id

async def _persist_external_codex_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint or update a child Conversation for a Codex AgentControl sub-agent.

    Idempotent: repeated POSTs for the same ``thread_id`` return the
    existing child id and upsert any new labels.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row.
    :param body: POST event body with ``data.thread_id`` required.
    :param conversation_store: Store for reading/creating child rows.
    :returns: Child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: If ``thread_id`` is missing or parent has
        no bound agent.
    """
    thread_id = body.data.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise OmnigentError(
            "external_codex_subagent_start requires non-empty data.thread_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a codex-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )
    existing = await asyncio.to_thread(
        _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
    )
    labels = _codex_subagent_labels_from_body(thread_id, body)
    if existing is not None:
        await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
        return existing.id
    return await _create_and_publish_codex_child(
        parent_id, parent_conv, thread_id, labels, conversation_store
    )

def _publish_terminal_pending(session_id: str, pending: bool) -> None:
    """
    Publish a typed :class:`SessionTerminalPendingEvent` and update the
    cache the snapshot reads.

    Every relay site that changes the terminal-spin-up flag funnels
    through here so the in-memory ``_session_terminal_pending_cache``
    stays coherent with the SSE stream — a client connecting
    mid-spin-up seeds the spinner from the snapshot's
    ``terminal_pending`` field, while already-connected clients update
    live off this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the runner is auto-creating the
        terminal; ``False`` once it lands or auto-create fails.
    """
    # Store only ``True`` entries; delete on clear so the cache never
    # accumulates stale ``False`` entries for every terminal-first session
    # that has ever completed spin-up. The snapshot getter uses
    # ``.get(id, False)`` so absent == False.
    if pending:
        _session_terminal_pending_cache[session_id] = True
    else:
        _session_terminal_pending_cache.pop(session_id, None)
    event = SessionTerminalPendingEvent(
        type="session.terminal_pending",
        conversation_id=session_id,
        pending=pending,
    )
    session_stream.publish(session_id, event.model_dump())

def _is_native_terminal_session(conv: Conversation) -> bool:
    """
    Return whether a session is owned by a terminal-native wrapper.

    :param conv: Conversation row for the target session.
    :returns: ``True`` for wrappers whose transcript forwarder is the
        single writer for conversation history.
    """
    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    return native_coding_agent_for_wrapper_label(wrapper) is not None

def _native_terminal_runtime(conv: Conversation) -> tuple[str, str, str]:
    """
    Return native terminal runtime strings for a wrapper session.

    :param conv: Conversation row for the target session.
    :returns: ``(display_name, model, harness)``.
    :raises OmnigentError: If the wrapper label is unsupported.
    """
    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    native_agent = native_coding_agent_for_wrapper_label(wrapper)
    if native_agent is not None:
        return native_agent.display_name, native_agent.agent_name, native_agent.harness
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )

def _native_terminal_name_for_harness(harness: str) -> str:
    """
    Return the runner terminal resource name for a native harness.

    :param harness: Native harness identifier, e.g. ``"codex-native"``.
    :returns: Terminal resource name, e.g. ``"codex"``.
    :raises OmnigentError: If *harness* is not a supported native
        terminal harness.
    """
    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is not None:
        return native_agent.terminal_name
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )

def _native_terminal_ensure_transport_error(
    exc: httpx.HTTPError | ConnectionError,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert runner transport failure during native terminal ensure.

    The message path has exactly one preflight path for native terminal
    readiness. If that path cannot reach the runner, fail the user turn
    explicitly instead of falling back to the old forward-and-wait path.

    :param exc: Transport exception from the ensure request, e.g.
        ``httpx.ConnectError("connection refused")`` or a bare
        ``ConnectionError`` raised by a runner transport.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    detail = str(exc).strip()
    message = f"Native {display_name} terminal ensure request failed."
    if detail:
        message = f"{message} {detail}"
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=message,
    )

class _NativeTerminalEnsureOutcome:
    """
    Result of a native terminal readiness probe.

    :param error: Error data when the runner definitively failed to
        create the terminal (fails the turn with a durable banner), or
        ``None`` when the terminal is ready / the failure was not
        definitive.
    :param policy_notice: Human-readable reason that tool-call policy
        enforcement is NOT active for this session (fail-open — codex too
        old or the hook could not be trusted), or ``None`` when
        enforcement is active. Non-fatal: surfaced once as a durable
        banner, never blocks the turn.
    """

    error: ErrorData | None
    policy_notice: str | None

async def _ensure_native_terminal_ready(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
) -> _NativeTerminalEnsureOutcome:
    """
    Ask the runner to create or return the native terminal for a message.

    The runner's explicit ``ensure_native_terminal`` endpoint is the
    authoritative readiness check for native user messages. Any non-2xx
    response or transport failure fails this user turn quickly with a
    durable error item; a 2xx response preserves the normal boot grace
    because the runner has accepted responsibility for terminal startup.
    A 2xx response may also carry ``policy_hook_disabled_reason`` — a
    one-shot, non-fatal notice that policy enforcement is inactive — which
    is returned as ``policy_notice`` for the caller to surface as a banner.

    :param runner_client: HTTP client pointed at the session's runner.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row used to identify the native harness.
    :returns: The probe outcome — a definitive ``error`` (terminal could
        not start) and/or a non-fatal ``policy_notice``.
    """
    display_name, _, harness = _native_terminal_runtime(conv)
    terminal_name = _native_terminal_name_for_harness(harness)
    # Cold-start tolerance (BDP-2579 extension): on a FRESH session the runner
    # is still launching its tmux terminal (~10s) when this preflight fires, so
    # the first request either (a) hits a runner not yet subscribed on the NATS
    # transport (NoRespondersError → ConnectError, immediate) or (b) the runner
    # accepts it but the in-handler tmux launch outlasts a short per-request
    # timeout. A single 10s attempt therefore races the launch and surfaces a
    # spurious ``native_terminal_ensure_failed`` that aborts the user's first
    # turn. Retry transport failures with a small backoff over a budget that
    # comfortably covers a cold tmux launch; a definitive non-2xx (real boot
    # failure) still fails fast below — only transport/timeout errors retry.
    last_exc: httpx.HTTPError | ConnectionError | None = None
    resp = None
    for attempt in range(_NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS):
        try:
            resp = await runner_client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={
                    "terminal": terminal_name,
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
                timeout=_NATIVE_TERMINAL_ENSURE_TIMEOUT_S,
            )
            break
        except (httpx.HTTPError, ConnectionError) as exc:
            last_exc = exc
            if attempt + 1 < _NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS:
                _logger.info(
                    "%s terminal ensure transport retry %d/%d for session=%s (runner warming up)",
                    display_name,
                    attempt + 1,
                    _NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS,
                    session_id,
                )
                await asyncio.sleep(_NATIVE_TERMINAL_ENSURE_RETRY_DELAY_S)
                continue
            # Runner transports may raise bare ConnectionError; without this clause
            # a runner transport drop escaped to the catch-all handler and the
            # web client showed an opaque 500 ``internal_error`` instead of
            # the durable ensure-failure turn error below.
            _logger.warning(
                "%s terminal ensure transport failed for session=%s after %d attempts",
                display_name,
                session_id,
                _NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS,
                exc_info=True,
            )
            return _NativeTerminalEnsureOutcome(
                error=_native_terminal_ensure_transport_error(last_exc, display_name=display_name),
                policy_notice=None,
            )
    assert resp is not None  # loop either set resp or returned
    if resp.status_code < 400:
        return _NativeTerminalEnsureOutcome(
            error=None,
            policy_notice=_policy_notice_from_ensure_response(resp),
        )
    _logger.warning(
        "%s terminal ensure failed definitively for session=%s status=%s body=%s",
        display_name,
        session_id,
        resp.status_code,
        resp.text[:500],
    )
    return _NativeTerminalEnsureOutcome(
        error=_native_terminal_failure_from_runner_response(resp, display_name=display_name),
        policy_notice=None,
    )

async def _persist_native_terminal_failure(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    error: ErrorData,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and terminal-start error.

    Used when a native terminal definitively cannot start. The AP
    server becomes the writer for this failure turn only: it records
    the user's message so the input is consumed, records a sibling
    ``type="error"`` item so refresh/reconnect can render the banner,
    and publishes the same live error/status events clients already
    understand.

    When the failing session is a native sub-agent, the parent's runner
    is also notified via an ``external_session_status: failed`` forward
    (see :func:`_forward_native_subagent_terminal_failure`). The native
    bypass returns HTTP 200 to the parent's runner ``spawn`` call, so
    without this forward the parent's work entry would stay ``running``
    forever — no harness boots, so no Stop hook ever fires the terminal
    edge the normal completion path relies on.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param error: Error data derived from the runner's ensure response.
    :param runner_router: Router used to resolve the (sub-agent's own)
        runner for the parent-wake forward, or ``None`` in
        in-process / test setups where the global client is used.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        user_item,
        conversation_store,
    )
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=turn_id,
            data=error,
        ),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(
        session_id,
        "failed",
        ErrorDetail(code=error.code, message=error.message),
    )
    # A boot failure on a native sub-agent must wake the parent — mirror
    # the normal terminal-status path (publish + forward), gated on
    # ``kind == "sub_agent"`` so top-level native sessions are unaffected.
    await _forward_native_subagent_terminal_failure(
        session_id,
        conv,
        error,
        runner_router,
    )
    return consumed.id

async def _forward_native_subagent_terminal_failure(
    session_id: str,
    conv: Conversation,
    error: ErrorData,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Wake the parent runner when a native sub-agent fails to boot its terminal.

    Mirrors the terminal-status path's parent-wake (the ``idle`` /
    ``failed`` branch of ``external_session_status`` in
    :func:`post_event`): forward an ``external_session_status: failed``
    edge — carrying the boot error as ``output`` so it lands in the
    parent's inbox — to the sub-agent's own runner, then require the
    forward to land. The runner's ``external_session_status`` handler
    maps ``failed`` to ``mark_subagent_work_terminal(status="failed")``,
    which marks the parent's work entry terminal and wakes the parent.

    No-ops for non-sub-agent sessions and for codex-internal sub-agents
    (tracked inside the same app-server thread tree, with no runner
    inbox entry to forward to — identical to the normal path's
    ``_is_codex_native_subagent`` exclusion).

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conv: Conversation row for the sub-agent session.
    :param error: Boot error to relay to the parent as the turn result.
    :param runner_router: Router used to resolve the sub-agent's runner,
        or ``None`` (then the global client is used).
    :returns: None.
    :raises OmnigentError: If the parent's runner could not be reached
        or rejected the forwarded failure status — dropping it would
        strand the parent waiting forever.
    """
    if conv.kind != "sub_agent" or _is_codex_native_subagent(conv):
        return
    forward_body: dict[str, Any] = {
        "type": _EXTERNAL_SESSION_STATUS_TYPE,
        # ``output`` is the parent-inbox result text on a failed edge
        # (runner: ``output or "...turn failed"``); pass the real error.
        "data": {"status": "failed", "output": error.message},
    }
    runner_result = await _forward_session_change_to_runner(
        session_id,
        runner_router,
        forward_body,
    )
    _require_external_status_forward(session_id, "failed", runner_result)

async def _persist_native_policy_notice(
    session_id: str,
    conversation_store: ConversationStore,
    reason: str,
) -> None:
    """
    Persist + publish a non-fatal "policy not enforced" banner.

    The runner reports (once, via the terminal-ensure success response)
    that a native codex session started but tool-call policy enforcement
    is inactive (fail-open: codex too old, or the policy hook could not be
    trusted). This records a durable ``type="error"`` banner so the web UI
    shows the degraded-security state across refresh/reconnect, and
    mirrors it as a live ``response.error`` event. Unlike
    :func:`_persist_native_terminal_failure` it does NOT consume the user
    message or mark the turn failed — the terminal is up and the message
    still forwards; this is an advisory notice only.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used for the durable append.
    :param reason: Human-readable cause from the runner, e.g. ``"Codex CLI
        0.128.0 is older than 0.129.0; upgrade codex to enforce tool-call
        policies."``.
    :returns: None.
    """
    error = ErrorData(
        source="execution",
        code=_NATIVE_POLICY_NOT_ENFORCED_CODE,
        message=f"Tool-call policy enforcement is not active for this session: {reason}",
    )
    persisted = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=generate_task_id(),
            data=error,
        ),
    )
    # Mirror to live clients only when newly persisted (the runner's
    # one-shot flag already prevents re-surfacing; this dedups a same-turn
    # retry against an already-recorded notice).
    if persisted == "persisted":
        _publish_error_event(session_id, error)

def _build_native_terminal_message_event(
    conv: Conversation,
    body: SessionEventInput,
) -> dict[str, Any]:
    """
    Build the runner event that delivers a web message to a native TUI.

    :param conv: Conversation row for the target session.
    :param body: Validated Sessions API message event, e.g.
        ``{"type": "message", "data": {"role": "user",
        "content": [{"type": "input_text", "text": "Hi"}]}}``.
    :returns: Harness ``MessageEvent`` body for the runner-local
        native terminal harness, including ``agent_id`` so the runner
        can resolve the harness spec on the first message.
    :raises OmnigentError: If the event is not a user message.
    """
    display_name, model, harness = _native_terminal_runtime(conv)
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    if not isinstance(data, MessageData) or data.role != "user":
        raise OmnigentError(
            f"{display_name} terminal sessions accept only user message events",
            code=ErrorCode.INVALID_INPUT,
        )
    return {
        "type": "message",
        "role": "user",
        "content": data.content,
        "model": model,
        "harness": harness,
        # The runner resolves the harness from the agent spec keyed by
        # agent_id; the forwarded ``harness`` hint is ignored on the turn
        # path. Without agent_id, the first message of a freshly
        # host-spawned runner (arriving before POST /v1/sessions caches
        # the spec) falls back to the test-only "runner-test-default"
        # harness and is dropped. Match the non-native forward path,
        # which always includes it.
        "agent_id": conv.agent_id,
    }

async def _forward_native_terminal_message(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
) -> None:
    """
    Forward one Omnigent web-chat message to the native terminal harness.

    The message is intentionally not persisted here. Claude Code
    and Codex record the accepted prompt in their terminal/app-server
    state, and their forwarders later post that terminal-originated
    item back through ``external_conversation_item``.

    :param runner_client: Runner client selected for ``session_id``.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*.
    :param body: Sessions API message event to inject.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references in ``input_image`` / ``input_file``
        content blocks.
    :param artifact_store: Optional binary content store for
        fetching file bytes during resolution.
    :returns: None.
    :raises HTTPException: 502 when the runner or harness rejects
        the injection request.
    """
    display_name, _, _ = _native_terminal_runtime(conv)
    event = _build_native_terminal_message_event(conv, body)
    _logger.info(
        "%s terminal message forward starting: session=%s block_types=%s",
        display_name,
        session_id,
        [block.get("type") for block in event.get("content", []) if isinstance(block, dict)]
        if isinstance(event.get("content"), list)
        else type(event.get("content")).__name__,
    )
    if (
        file_store is not None
        and artifact_store is not None
        and isinstance(event.get("content"), list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        try:
            event["content"] = _resolve_message_content(
                event["content"],
                file_store,
                artifact_store,
                session_id=session_id,
            )
        except (ValueError, KeyError):
            _logger.warning(
                "File reference resolution failed for native session=%s",
                session_id,
                exc_info=True,
            )
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S,
        )
        _logger.info(
            "%s terminal message runner response: session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text[:500],
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # Runner transports may raise bare ConnectionError; map it to the
        # same 502 as an httpx transport failure so a runner drop mid-forward
        # doesn't escape as an opaque 500.
        _logger.warning(
            "%s terminal message forward failed for session=%s",
            display_name,
            session_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed",
        ) from exc
    if resp.status_code >= 400:
        _logger.warning(
            "%s terminal message forward rejected for session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed ({resp.status_code})",
        )
    failure = _extract_claude_native_runner_failure(resp)
    if failure is not None:
        _logger.warning(
            "%s terminal message forward failed in runner SSE for session=%s: %s",
            display_name,
            session_id,
            failure,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed: {failure}",
        )

def _agent_is_native(agent: Agent) -> bool:
    """Return whether an agent runs a native CLI harness.

    Loads the agent's spec to read its ``harness_kind``. A native target
    (claude-native / codex-native) is the only case where a fork needs the
    transcript-rebuild carry path — SDK targets replay the Omnigent
    transcript as context on their own. Returns ``False`` when the bundle
    can't be loaded (treated as non-native).

    :param agent: The agent whose harness to classify.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    from omnigent.harness_aliases import is_native_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → treat as non-native
        return False
    return is_native_harness(spec.executor.harness_kind)

def _native_coding_agent_for_agent(agent: Agent) -> NativeCodingAgent | None:
    """
    Return native coding-agent metadata for an agent's harness.

    :param agent: The agent whose bundle should be inspected.
    :returns: Registry metadata for the native TUI harness, or ``None``.
    """
    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → non-native presentation
        return None
    return native_coding_agent_for_harness(spec.executor.harness_kind)

_MAX_TERMINAL_LAUNCH_ARGS = 256

_MAX_TERMINAL_LAUNCH_ARG_LEN = 4096

def _validate_terminal_launch_args(value: list[str] | None) -> list[str] | None:
    """
    Validate per-session native-terminal pass-through args.

    Enforces a flat list of strings within bounded count / length.
    The flat-list shape is the security boundary: there is no key for
    a caller to smuggle internal launch wiring (bridge dir, Omnigent URL,
    auth) through — those stay runner-owned (see
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md).

    :param value: The candidate args, e.g.
        ``["--dangerously-skip-permissions"]``, or ``None`` to leave
        unset / unchanged.
    :returns: The validated list unchanged, or ``None`` when *value*
        is ``None``.
    :raises ValueError: If *value* is not a list of strings, exceeds
        :data:`_MAX_TERMINAL_LAUNCH_ARGS` entries, or any entry
        exceeds :data:`_MAX_TERMINAL_LAUNCH_ARG_LEN` characters.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        raise ValueError("terminal_launch_args must be a list of strings")
    if len(value) > _MAX_TERMINAL_LAUNCH_ARGS:
        raise ValueError(f"terminal_launch_args exceeds {_MAX_TERMINAL_LAUNCH_ARGS} entries")
    for arg in value:
        if len(arg) > _MAX_TERMINAL_LAUNCH_ARG_LEN:
            raise ValueError(
                f"terminal_launch_args entry exceeds {_MAX_TERMINAL_LAUNCH_ARG_LEN} characters"
            )
    return value

def _derive_terminal_launch_args_from_spec(sub_spec: AgentSpec) -> list[str] | None:
    """
    Derive native-terminal YOLO pass-through args from a trusted sub-spec.

    polly's native workers (claude-native / codex-native) launch in a
    headless pane where no human can answer an ApprovalCard, so every
    Edit/Write/Bash that prompts stalls the worker. This translates a
    worker bundle's declared full-bypass intent into the per-session
    ``terminal_launch_args`` the runner already appends to the claude /
    codex argv:

    - claude-native + ``executor.config.permission_mode`` set ->
      ``["--permission-mode", "<value>"]``. The value is passed through
      verbatim so non-YOLO modes (``acceptEdits``, ``plan``, ...) work too;
      YOLO uses ``bypassPermissions``.
    - codex-native + ``executor.config.yolo`` truthy ->
      ``["--dangerously-bypass-approvals-and-sandbox"]``.

    Only the two native harnesses are translated; for any other harness
    (e.g. ``claude-sdk``, whose bypass is set via the SDK ``permissionMode``
    spawn env, not a terminal flag) this returns ``None`` so no terminal
    args are set. ``None`` is also returned when the relevant field is
    absent / falsey.

    :param sub_spec: The trusted child sub-agent spec, resolved from the
        server-loaded parent bundle via :func:`_resolve_subagent_spec`.
    :returns: A flat CLI-arg list to store as the child session's
        ``terminal_launch_args``, or ``None`` when nothing should be set.
    :raises ValueError: If a spec-derived argument violates the same
        bounds enforced for request-supplied ``terminal_launch_args``.
    """
    harness = _spec_harness(sub_spec)
    if harness == _CLAUDE_NATIVE_HARNESS:
        permission_mode = sub_spec.executor.config.get("permission_mode")
        if permission_mode:
            return _validate_terminal_launch_args(["--permission-mode", str(permission_mode)])
        return None
    if harness == _CODEX_NATIVE_HARNESS:
        if _spec_config_flag_enabled(sub_spec, "yolo"):
            return _validate_terminal_launch_args(["--dangerously-bypass-approvals-and-sandbox"])
        return None
    return None

def _native_subagent_wrapper_labels_from_spec(sub_spec: AgentSpec) -> dict[str, str]:
    """
    Resolve terminal-first wrapper labels from an already-loaded sub-spec.

    :param sub_spec: Trusted child sub-agent spec resolved from the
        parent bundle.
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when the sub-agent is not native.
    """
    harness = _spec_harness(sub_spec)
    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is not None:
        return {
            _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: native_agent.wrapper_label,
            _CLAUDE_NATIVE_UI_LABEL_KEY: _CLAUDE_NATIVE_UI_LABEL_VALUE,
        }
    return {}

def _native_subagent_wrapper_labels(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> dict[str, str]:
    """
    Resolve the terminal-first wrapper labels for a native-harness sub-agent.

    A sub-agent dispatched via ``sys_session_send`` whose own spec uses a
    native terminal harness (``claude-native`` / ``codex-native``) must
    render with the Chat/Terminal pill in the web UI, exactly like a
    top-level ``claude-native-ui`` / ``codex-native-ui`` wrapper session.
    The pill is gated on the conversation's ``omnigent.wrapper`` +
    ``omnigent.ui`` labels (see ``ap-web`` ``TerminalFirstContext``), but
    the sub-agent create path never stamps them. This resolves the child
    sub-agent's spec from the parent bundle and returns the labels to stamp,
    or an empty dict when the sub-agent is not native (e.g. ``claude-sdk``).

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns an empty dict).
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when not native / not resolvable.
    """
    sub_spec = _resolve_subagent_spec(
        agent=agent,
        sub_agent_name=sub_agent_name,
        agent_cache=agent_cache,
    )
    if sub_spec is None:
        return {}
    return _native_subagent_wrapper_labels_from_spec(sub_spec)

