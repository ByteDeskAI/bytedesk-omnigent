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


def _sessions_facade():
    from omnigent.server.routes import sessions

    return sessions


def _allow_all_edits_eligible(tool_name: str, permission_mode: str | None) -> bool:
    """
    Whether a claude-native PermissionRequest may offer / honor the
    "Accept & allow all edits" affordance.

    Eligible for file-editing tools under a mode that still prompts,
    and for ``ExitPlanMode`` — accepting a plan with the flag is the
    plan card's "Yes, and use auto mode" option (exit plan mode AND
    switch the session into Claude's ``auto`` mode).
    Already-permissive modes (``acceptEdits`` / ``bypassPermissions``)
    wouldn't prompt at all, so the switch would be inert. Used at BOTH
    the stamp site (drives the UI button) and the verdict site (gates
    the ``setMode`` decision), so the server never honors a
    client-supplied ``allow_all_edits`` flag on a tool/mode the
    affordance was never offered for.

    :param tool_name: The gated tool from Claude's PermissionRequest
        payload, e.g. ``"Edit"`` or ``"Bash"``.
    :param permission_mode: Claude's current permission mode from the
        payload, e.g. ``"default"`` / ``"plan"`` / ``"acceptEdits"`` /
        ``None`` when absent.
    :returns: ``True`` iff the affordance applies.
    """
    return (
        tool_name in _CLAUDE_NATIVE_EDIT_TOOLS or tool_name == "ExitPlanMode"
    ) and permission_mode not in (
        "acceptEdits",
        "bypassPermissions",
    )

_RACE_TASK_REAP_TIMEOUT_S = 5.0

_SESSION_STREAM_HEARTBEAT_INTERVAL_S = 15.0

async def _poll_request_disconnect(request: Request) -> None:
    """
    Resolve once Starlette reports the client closed the connection.

    Long-poll routes that park on a verdict (e.g. the Claude-native
    ``PermissionRequest`` hook) use this to detect that the upstream
    client has hung up — Claude closes its HTTP request when its
    TUI prompt receives an answer first, and without this wait the
    handler would sit out the full timeout to notice.

    Blocks on ``request.receive()`` rather than polling
    ``request.is_disconnected()``. The poll variant runs each check
    inside a pre-cancelled anyio ``CancelScope`` (Starlette's
    non-blocking receive idiom); an external ``Task.cancel()`` that
    lands while that scope is unwinding coalesces with the scope's own
    cancellation and is swallowed with it, so the poller survives its
    cancel and the caller's race cleanup blocks on it forever.
    A blocking receive has no cancel scope in its await chain, so
    cancellation always propagates; it is also cheaper than waking
    twice a second.

    :param request: The active FastAPI :class:`Request`. By the time
        the handler parks, the route has consumed the body, so the
        next receive yields only ``http.disconnect``.
    :returns: None when the disconnect is observed. Cancellation
        propagates: callers that race this against a verdict Future
        cancel the wait once the verdict arrives.
    """
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return

def _session_status_from_cache(conversation_id: str) -> Literal["idle", "running", "failed"]:
    """
    Map the relay-fed status cache value to a list-item status.

    The cache stores the fine-grained relay status (``"running"``,
    ``"waiting"``, ``"failed"``, ``"idle"``); the list-item shape
    collapses ``"running"``/``"waiting"`` to ``"running"``. A cache
    miss means no relay has reported on this session, which presents
    as ``"idle"``.

    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"``.
    """
    cached = _session_status_cache.get(conversation_id)
    if cached in ("running", "waiting"):
        return "running"
    if cached == "failed":
        return "failed"
    return "idle"

def _session_status_with_child_rollup(
    conversation_id: str,
    child_session_ids: list[str],
) -> Literal["idle", "running", "failed"]:
    """
    Map a session's cached status plus direct child activity to list status.

    A parent session should read as ``"running"`` in the sidebar while any
    direct sub-agent child is still ``"running"`` or ``"waiting"``, even if
    the parent runner has already gone idle. This keeps every sidebar row
    honest without mounting a child-session query for each row.

    :param conversation_id: Parent session/conversation identifier,
        e.g. ``"conv_parent123"``.
    :param child_session_ids: Direct sub-agent child conversation ids,
        e.g. ``["conv_child1", "conv_child2"]``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"`` for the
        session-list row.
    """
    own_status = _session_status_from_cache(conversation_id)
    if own_status == "running":
        return "running"
    if any(
        _session_status_cache.get(child_id) in ("running", "waiting")
        for child_id in child_session_ids
    ):
        return "running"
    return own_status

def _agent_display_names_for(
    agent_ids: list[str],
    agent_store: AgentStore,
    agent_cache: AgentCache | None,
) -> dict[str, str | None]:
    """
    Resolve human display names (``params.displayName``) for a set of agents.

    Mirrors the read-time projection in ``_to_agent_object`` /
    ``GET /v1/agents`` so session-bound list rows render the person's name
    (e.g. ``"Maya Chen"``) instead of the slug. Loads each agent's spec via
    the shared :class:`AgentCache` (cache hits after first load), deduped by
    the caller passing distinct ids. Best-effort: a missing row or a spec that
    fails to load is simply omitted (the client falls back to the slug) and
    never breaks the session list.

    :param agent_ids: Distinct agent ids to resolve.
    :param agent_store: Store to fetch the agent row (for its bundle location).
    :param agent_cache: Shared spec cache; ``None`` disables resolution.
    :returns: Map from agent id to display name; ids with no
        ``params.displayName`` are omitted.
    """
    out: dict[str, str | None] = {}
    if agent_cache is None:
        return out
    for aid in agent_ids:
        try:
            agent = agent_store.get(aid)
            if agent is None:
                continue
            loaded = agent_cache.load(
                agent.id, agent.bundle_location, expand_env=agent.session_id is None
            )
            params = loaded.spec.params or {}
            if isinstance(params, dict):
                dn = params.get("displayName")
                if dn:
                    out[aid] = str(dn)
        except Exception:  # noqa: BLE001 — never break the list on one bad spec
            _logger.debug("display_name resolution failed for agent %s", aid, exc_info=True)
    return out

def _resolve_llm_model(conv: Conversation | None) -> str | None:
    """
    Resolve the LLM model identifier from a conversation's agent spec.

    Uses the global agent cache to load the parsed spec and read
    ``spec.llm.model``. Returns ``None`` when the conversation has
    no agent binding or the spec cannot be loaded.

    :param conv: The conversation entity, or ``None``.
    :returns: Model string (e.g. ``"databricks-gpt-5-5"``), or
        ``None`` when unavailable.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
        # The agent store is injected at app startup; access it
        # through the runtime globals.
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        return loaded.spec.llm.model if loaded.spec.llm else None
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None

def _resolve_output_schema(conv: Conversation | None) -> dict[str, Any] | None:
    """
    Resolve the bound agent's declared ``output_schema`` (BDP-2393).

    Mirrors :func:`_resolve_llm_model`: loads the bound agent's parsed
    spec from the cache and returns its structured-output JSON Schema, or
    ``None`` when the session has no agent binding, the spec can't be
    loaded, or no ``output_schema`` was declared (free-text default).

    :param conv: The conversation entity, or ``None``.
    :returns: The JSON Schema mapping, or ``None``.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        return loaded.spec.output_schema
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None

def _resolve_harness(conv: Conversation | None) -> str | None:
    """
    Resolve the canonical harness for a conversation's bound agent.

    Mirrors :func:`_resolve_llm_model`: loads the parsed spec via the agent
    cache and returns the executor's harness
    (``executor.config["harness"]``, else ``executor.type``), canonicalized.
    Surfacing this on :class:`SessionResponse` lets the REPL render the
    active credential for the correct provider *family* — anthropic for
    claude-sdk, openai for codex / openai-agents — instead of guessing the
    family from the model string (which is wrong when the agent declares no
    model, e.g. a generic-provider launcher).

    :param conv: The conversation entity, or ``None``.
    :returns: The canonical harness (e.g. ``"openai-agents"`` or
        ``"claude-sdk"``), or ``None`` when unavailable.
    """
    if conv is None:
        return None
    # A persisted per-session override (validated + canonicalized at
    # create) wins over the spec's declared harness, so the snapshot
    # reports what the runner actually spawns.
    if conv.harness_override:
        return conv.harness_override
    if conv.agent_id is None:
        return None
    try:
        from omnigent.harness_aliases import canonicalize_harness
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        executor = loaded.spec.executor
        harness = executor.config.get("harness") or executor.type
        return canonicalize_harness(harness) or harness
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None

_MODEL_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

def _coerce_cumulative_field(
    data: dict[str, Any],
    key: str,
    *,
    numeric: bool,
) -> float | int | None:
    """
    Read and validate an optional cumulative usage field from event data.

    :param data: The ``external_session_usage`` event ``data`` dict.
    :param key: Field name, e.g. ``"cumulative_input_tokens"``.
    :param numeric: When ``True`` accept any non-negative number (cost);
        when ``False`` require a non-negative int (token counts).
    :returns: The validated value, or ``None`` when the key is absent.
    :raises OmnigentError: When present but the wrong type / negative.
    """
    value = data.get(key)
    if value is None:
        return None
    ok = (
        isinstance(value, (int, float)) if numeric else isinstance(value, int)
    ) and not isinstance(value, bool)
    if not ok or value < 0:
        raise OmnigentError(
            f"external_session_usage data.{key} must be a non-negative "
            f"{'number' if numeric else 'int'}",
            code=ErrorCode.INVALID_INPUT,
        )
    return value

async def _persist_model_change_note(
    session_id: str,
    model_override: str | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Append a ``[System: ...]`` transcript note recording a model switch.

    Records a web/REPL ``/model`` change as a user-role system marker
    (the web UI renders ``[System: ...]`` user messages centered + muted
    via ``SystemMessageView``) so the user gets a durable record in the
    conversation that the switch happened — not just a transient composer
    hint. Persisted through the store as append-only history (does NOT
    start an agent turn, unlike the message-post path) and published over
    SSE so connected clients render it live.

    The caller gates this to **non-native** sessions (those WITHOUT an
    ``omnigent.wrapper`` native label, via ``_is_native_terminal_session``)
    and to real ``/model`` commands: claude-native / codex-native manage
    their model through the in-TUI picker / launch flag and must not receive
    an injected AP-side item, and ``silent`` bind-time auto-applies are
    skipped (see the ``live_forward`` guard in ``update_session``). The gate
    keys on ``omnigent.wrapper`` rather than ``omnigent.ui == "terminal"``
    because the latter is also set on chat-first SDK sessions that expose a
    REPL terminal view (e.g. polly / debby), which DO want the note. The note
    is a user-role message, so the agent sees it in history on the next turn —
    consistent with other ``[System: ...]`` markers (timer fired, sub-agent
    done).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param model_override: The new model id, e.g.
        ``"databricks-gpt-5-4"``, or ``None`` when the override was
        cleared back to the agent default.
    :param conversation_store: Store used to append the note item.
    :returns: None.
    """
    text = (
        f"[System: model changed to {model_override}]"
        if model_override is not None
        else "[System: model reset to the agent default]"
    )
    item = NewConversationItem(
        type="message",
        response_id=generate_task_id(),
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    _publish_external_conversation_item(session_id, persisted_items[0])

def _merge_pending_file_blocks(
    item: NewConversationItem,
    pending_content: list[dict[str, Any]],
) -> NewConversationItem:
    """
    Prepend a pending entry's file blocks onto a user-message item.

    The claude-native transcript mirrors a user message back as
    text-only — ``input_image`` / ``input_file`` blocks are dropped. The
    optimistic pending-input entry still carries them (with real
    ``file_id``s, assigned at upload), so we fold them into the durable
    item here. Without it the image renders only on the optimistic
    bubble and vanishes from history on the next reload.

    No-op when the pending entry has no file blocks, or when the item
    already carries file blocks (defensive — a future transcript that
    does include them must not be doubled).

    :param item: The parsed user-message item about to be persisted.
        Its ``data`` is a :class:`MessageData` whose ``content`` is a
        list of block dicts, e.g. ``[{"type": "input_text",
        "text": "hi"}]``.
    :param pending_content: The drained pending entry's content blocks,
        e.g. ``[{"type": "input_image", "file_id": "file_x",
        "filename": "a.png"}, {"type": "input_text", "text": "hi"}]``.
    :returns: A copy of *item* with the file blocks prepended, or *item*
        unchanged when there is nothing to merge.
    """
    if not isinstance(item.data, MessageData):
        return item
    file_blocks = [
        block
        for block in pending_content
        if isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
    ]
    if not file_blocks:
        return item
    already_has_files = any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
        for block in item.data.content
    )
    if already_has_files:
        return item
    merged_data = item.data.model_copy(update={"content": [*file_blocks, *item.data.content]})
    return item.model_copy(update={"data": merged_data})

def _message_text(content: list[dict[str, Any]]) -> str | None:
    """
    Extract joined text from message content blocks.

    :param content: Message content blocks, e.g.
        ``[{"type": "output_text", "text": "Done"}]``.
    :returns: Joined text from ``text`` / ``input_text`` fields,
        or ``None`` when no text field exists.
    """
    parts: list[str] = []
    found_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("input_text")
        if isinstance(text, str):
            found_text = True
            parts.append(text)
    return "\n".join(parts) if found_text else None

def _latest_assistant_text_from_store(
    conversation_store: ConversationStore,
    session_id: str,
) -> str | None:
    """
    Return the latest persisted assistant message text for a session.

    Native harnesses mirror completed transcript items to the AP
    server, not necessarily to the runner's in-memory history. This
    helper lets Omnigent forward the durable assistant output with the
    terminal-observed idle edge.

    :param conversation_store: Store used to read conversation items.
    :param session_id: Session/conversation id, e.g.
        ``"conv_child123"``.
    :returns: Latest assistant text, or ``None`` when none is
        persisted yet.
    """
    page = conversation_store.list_items(
        session_id,
        limit=_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
        order="desc",
        type="message",
    )
    for item in page.data:
        if not isinstance(item.data, MessageData):
            continue
        if item.data.role != "assistant" or item.data.is_meta:
            continue
        text = _message_text(item.data.content)
        if text is not None:
            return text
    return None

async def _persist_session_status_error_labels(
    session_id: str,
    error: ErrorDetail | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist or clear the reload-visible failure detail for a session status.

    ``session.status`` is an SSE edge, so its ``error`` object disappears on
    reload. Terminal-native sessions can fail before any transcript item is
    written, so store the latest failure detail as runner-owned labels and let
    snapshots project it as ``last_task_error``. Empty string clears stale
    values because the label store is upsert-only.

    :param session_id: Session/conversation identifier.
    :param error: Failure detail from a ``session.status: failed`` edge, or
        ``None`` to clear stale error labels on subsequent activity.
    :param conversation_store: Store used to upsert labels.
    """
    updates = (
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: error.code,
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: error.message,
        }
        if error is not None
        else {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: "",
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "",
        }
    )
    try:
        await asyncio.to_thread(conversation_store.set_labels, session_id, updates)
    except Exception:
        _logger.exception(
            "Failed to persist session status error labels for %s",
            session_id,
        )

def _last_task_error_from_labels(labels: Mapping[str, str]) -> dict[str, str] | None:
    """
    Project runner-owned failure labels into the typed API error shape.

    Terminal/native runtimes can fail before they write any transcript item,
    so the session-status relay stores the latest failure as durable labels.
    This helper is the single server-side boundary where those internal labels
    become public ``last_task_error`` data for snapshots and child summaries.

    :param labels: Conversation labels, usually after closed-status projection.
    :returns: ``{"code": "...", "message": "..."}``, or ``None`` when either
        value is absent/cleared.
    """
    raw_error_code = labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY)
    raw_error_message = labels.get(_LAST_TASK_ERROR_MESSAGE_LABEL_KEY)
    if raw_error_code and raw_error_message:
        return {
            "code": raw_error_code,
            "message": raw_error_message,
        }
    return None

async def _validate_session_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    request: Request,
) -> str:
    """
    Validate a session's workspace against the agent's os_env boundary.

    Wraps the seven-step validation in
    :mod:`omnigent.server.routes._workspace_validation` and
    raises :class:`OmnigentError` on failure so the route layer
    converts the error into a 400 response with a clear message.
    See ``designs/SESSION_WORKSPACE_SELECTION.md`` for the full
    semantic spec.

    The caller's host ownership is checked BEFORE the ``host.stat``
    round-trip the validation performs, so a non-owner never reaches
    another user's host (raises 403/404 via ``resolve_host_owner``).

    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``, or ``None`` when auth is disabled.
    :param host_id: Stable host id, e.g. ``"host_a1b2c3d4..."``.
    :param workspace: Absolute path supplied by the caller, e.g.
        ``"/Users/corey/universe/src/foo"``. ``None`` is rejected
        with the "workspace required when host_id is set" message.
    :param agent: The agent the session binds to. Used to load the
        bundle and read ``os_env.cwd`` for boundary computation.
    :param agent_cache: Cache for loading parsed agent specs from
        bundle storage. Required because session-create needs the
        spec; ``None`` is treated as a server config error.
    :param request: FastAPI request; ``request.app.state``
        carries the host registry and host store.
    :returns: The canonicalized workspace path that should be
        stored on the session row, e.g.
        ``"/Users/corey/universe/src/foo"`` (realpath; symlinks
        already resolved by the host).
    :raises OmnigentError: With ``ErrorCode.INVALID_INPUT`` on
        any validation failure (offline host, missing path,
        outside boundary, missing subdir). With
        ``ErrorCode.INTERNAL_ERROR`` if ``agent_cache`` is unset.
    """
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    if workspace is None:
        raise OmnigentError(
            "workspace required when host_id is set",
            code=ErrorCode.INVALID_INPUT,
        )
    if not workspace.startswith("/"):
        raise OmnigentError(
            "workspace must be an absolute path starting with /",
            code=ErrorCode.INVALID_INPUT,
        )
    if agent_cache is None:
        # Should never happen in production — the route factory
        # always wires an agent cache. Fail loud rather than
        # silently skipping validation, which would let bad
        # workspaces through.
        raise OmnigentError(
            "workspace validation requires an agent cache",
            code=ErrorCode.INTERNAL_ERROR,
        )

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )

    # Authorize host ownership FIRST — before loading the agent spec or
    # the host.stat round-trip below. A non-owner must be rejected
    # (403/404 via the shared resolve_host_owner) before we touch the
    # host or even read the agent bundle (cross-user host probe). The
    # returned host also gives the display name for error messages.
    from omnigent.server.routes._host_launch import resolve_host_owner

    host_name: str | None = None
    host_store_inst = getattr(request.app.state, "host_store", None)
    if host_store_inst is not None:
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store_inst,
        )
        host_name = host.name

    # Read the agent's os_env.cwd — None when the spec has no
    # os_env block (headless agents). Headless agents have no
    # filesystem access at all but still get launched on hosts
    # for sessions that don't need it; treat their cwd as
    # relative-equivalent so the boundary is unrestricted.
    spec_cwd: str | None = None
    if agent.bundle_location is not None:
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
            )
            os_env = getattr(loaded.spec, "os_env", None)
            spec_cwd = getattr(os_env, "cwd", None) if os_env is not None else None
        except Exception as exc:
            _logger.exception("Failed to load agent spec for workspace validation")
            raise OmnigentError(
                f"failed to load agent spec: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

    try:
        return await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=spec_cwd,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc

async def _record_host_cooldown(host_id: str, cooldown_s: float) -> None:
    """Mark a wedged host as cooled-down so the selector skips it (BDP-2579 F5)."""
    from omnigent.db.utils import now_epoch

    expires_at = now_epoch() + int(cooldown_s)
    try:
        from omnigent.coordination.lifecycle import get_active_backplane

        backplane = get_active_backplane()
    except Exception:  # noqa: BLE001 — coordination optional
        backplane = None
    if backplane is not None:
        with contextlib.suppress(Exception):
            await backplane.index_put(
                "registry", f"host-cooldown.{host_id}", {"expires_at": expires_at}
            )
            return
    _sessions_facade()._host_cooldowns[host_id] = expires_at

async def _hosts_in_cooldown() -> set[str]:
    """Set of host ids still within their circuit-breaker cooldown window."""
    from omnigent.db.utils import now_epoch

    now = now_epoch()
    try:
        from omnigent.coordination.lifecycle import get_active_backplane

        backplane = get_active_backplane()
    except Exception:  # noqa: BLE001
        backplane = None
    out: set[str] = set()
    if backplane is not None:
        with contextlib.suppress(Exception):
            entries = await backplane.index_list_prefix("registry", "host-cooldown.")
            for key, val in entries.items():
                exp = val.get("expires_at")
                if isinstance(exp, int) and exp > now:
                    out.add(key[len("host-cooldown.") :])
            return out
    cooldowns = _sessions_facade()._host_cooldowns
    for host_id, exp in list(cooldowns.items()):
        if exp > now:
            out.add(host_id)
        else:
            cooldowns.pop(host_id, None)
    return out

async def _failover_to_new_host(
    *,
    conv: Conversation,
    owner: str | None,
    bad_host_id: str,
    expected_runner: str,
    wedged: bool,
    cfg: RunnerHealConfig,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    runner_router: RunnerRouter | None,
    runner_control_registry: Any | None,
    runner_credential_store: Any | None,
    runner_exit_reports: RunnerExitReports | None,
) -> bool:
    """Rung 2: select a live capability-matching host and atomically repin.

    Trips the bad-host circuit-breaker ONLY when the failure was the
    ``acked=False`` host-wedge (a healthy host can run a crash-looping runner —
    never cooldown for a runner-only failure). Repins ``(host_id, runner_id)``
    together via ``cas_host_and_runner`` so the pair never splits across a hop
    (BDP-2579 F4/F5).
    """
    from omnigent.stores.host_store import LiveHostSelector

    if wedged:
        await _record_host_cooldown(bad_host_id, cfg.failover_host_cooldown_s)
        # Evict only on the owning replica; a host owned by another replica is
        # excluded via the shared cooldown index instead.
        if host_registry is not None:
            bad_conn = host_registry.get(bad_host_id)
            if bad_conn is not None:
                with contextlib.suppress(Exception):
                    host_registry.evict(bad_conn)

    selector = LiveHostSelector()
    harness = _resolve_harness(conv)
    excluded = {bad_host_id} | await _hosts_in_cooldown()
    current_host = bad_host_id
    current_runner = expected_runner

    for _hop in range(max(cfg.failover_max_hops, 1)):
        candidates = await asyncio.to_thread(host_store.list_hosts, owner)
        target = selector.select(
            candidates, harness=harness, exclude_host_ids=excluded
        )
        if target is None:
            return False

        def _repin(
            new_rid: str,
            _eh: str = current_host,
            _er: str = current_runner,
            _nh: str = target.host_id,
        ) -> bool:
            return conversation_store.cas_host_and_runner(
                conv.id, _eh, _er, _nh, new_rid
            )

        if host_registry is None:
            return False
        attempt = await _sessions_facade()._launch_runner_on_host_id(
            conv,
            conversation_store,
            host_registry,
            target.host_id,
            owner=owner,
            runner_control_registry=runner_control_registry,
            runner_credential_store=runner_credential_store,
            repin=_repin,
        )
        if not attempt.repinned:
            client = await _sessions_facade()._wait_for_runner_client(
                session_id=conv.id,
                runner_router=runner_router,
                runner_control_registry=runner_control_registry,
                runner_id=None,
                timeout_s=cfg.reconnect_hold_timeout_s,
                runner_exit_reports=runner_exit_reports,
            )
            return client is not None
        # The row now points at (target.host_id, attempt.runner_id).
        current_host = target.host_id
        current_runner = attempt.runner_id
        if not attempt.acked:
            # The failover target is also wedged — cooldown it and hop again.
            await _record_host_cooldown(target.host_id, cfg.failover_host_cooldown_s)
            if host_registry is not None:
                tconn = host_registry.get(target.host_id)
                if tconn is not None:
                    with contextlib.suppress(Exception):
                        host_registry.evict(tconn)
            excluded = {bad_host_id, target.host_id} | await _hosts_in_cooldown()
            continue
        client = await _sessions_facade()._wait_for_runner_client(
            session_id=conv.id,
            runner_router=runner_router,
            runner_control_registry=runner_control_registry,
            runner_id=attempt.runner_id,
            timeout_s=cfg.relaunch_attempt_timeout_s,
            runner_exit_reports=runner_exit_reports,
        )
        if client is not None:
            _logger.info(
                "Session %s failed over from host %s to host %s (runner %s)",
                conv.id,
                bad_host_id,
                target.host_id,
                attempt.runner_id,
            )
            return True
        excluded = {bad_host_id, target.host_id} | await _hosts_in_cooldown()
    return False

def _build_new_item(
    body: SessionEventInput,
    response_id: str,
    created_by: str | None = None,
) -> NewConversationItem:
    """
    Construct a :class:`NewConversationItem` from a POSTed event.

    Validates the data payload via ``parse_item_data`` (the same
    validator the route boundary already invoked) and wraps the
    result with the response_id linkage required by the conversation
    store.

    :param body: Validated event input — guaranteed to be a known
        item type (the route checked ``_ALLOWED_EVENT_TYPES``).
    :param response_id: The task id the new item should be tagged
        with — either the steered active task or a freshly-created
        one.
    :param created_by: Authenticated identity of the actor posting
        the event, recorded for per-message attribution. ``None`` in
        single-user mode.
    :returns: A :class:`NewConversationItem` ready for delivery
        or persistence.
    """
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    return NewConversationItem(
        type=body.type,
        response_id=response_id,
        data=data,
        created_by=created_by,
    )

def _title_content_from_item(item: NewConversationItem) -> list[dict[str, Any]]:
    """
    Extract title candidate content blocks from a session item.

    Only user ``message`` items contribute. Tool results and
    assistant-shaped messages return an empty list so callers leave
    the conversation title unchanged.

    :param item: The parsed item being persisted, e.g. a user
        ``"message"`` item with input text content.
    :returns: Content blocks that may contribute to a synthesized
        title, e.g. ``[{"type": "input_text", "text": "Hello"}]``.
    """
    if item.type != "message":
        return []
    if not isinstance(item.data, MessageData):
        return []
    if item.data.role != "user":
        return []
    return item.data.content

async def _seed_missing_title(
    conv: Conversation,
    content: list[dict[str, Any]],
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled conversation's title from message content blocks.

    No-op when the conversation already has a title or the blocks
    yield no usable text. Mutates ``conv.title`` in place on success
    so callers holding the row see the persisted value.

    :param conv: The conversation row for the session.
    :param content: Title-candidate blocks, e.g.
        ``[{"type": "input_text", "text": "/debate kafka vs sqs"}]``.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    if conv.title is not None:
        return
    title = synthesize_conversation_title(content)
    if title is None:
        return
    updated = await asyncio.to_thread(
        conversation_store.update_conversation,
        conv.id,
        title=title,
    )
    if updated is not None:
        conv.title = updated.title

async def _seed_missing_title_from_user_message(
    conv: Conversation,
    item: NewConversationItem,
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled session's title from a user message.

    The app UI creates sessions with ``initial_items=[]`` and posts
    the first user message through ``POST /v1/sessions/{id}/events``.
    This helper also covers callers that pass initial items to
    ``POST /v1/sessions``. Non-user-message items are ignored, and
    already-titled conversations are left unchanged.

    :param conv: The conversation row for the session.
    :param item: The parsed item being persisted.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    await _seed_missing_title(conv, _title_content_from_item(item), conversation_store)

async def _persist_session_event(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist a user event without forwarding to a runner.

    Used when the runner isn't online yet but the session has a
    ``host_id`` — the message is stored so the runner's crash-
    recovery block picks it up from history when it connects.

    :param session_id: Session/conversation identifier.
    :param body: The validated event input.
    :param conversation_store: Store for item persistence.
    :param agent_name: Agent name for title seeding.
    :returns: The store-assigned item id.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        session_id,
    )
    if conv is not None:
        await _seed_missing_title_from_user_message(
            conv,
            item,
            conversation_store,
        )
    item_id = persisted_items[0].id if persisted_items else turn_id
    _publish_external_conversation_item(session_id, persisted_items[0])
    return item_id

@dataclass
class _SessionEventDispatchResult:
    """
    Outcome of forwarding one item-event to the runner.

    :param item_id: Store-assigned id of the AP-persisted item, e.g.
        ``"item_abc123"``. ``None`` for the claude-native message
        bypass, which persists nothing AP-side.
    :param pending_id: Id of the :mod:`omnigent.runtime.pending_inputs`
        entry recorded for a native-terminal web message, e.g.
        ``"pending_a1b2c3"`` — surfaced to the sender so it can adopt
        the id and dedupe against the snapshot. ``None`` for non-native
        events (already persisted, so no separate pending entry).
    """

    item_id: str | None
    pending_id: str | None

def _extract_persistent_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Extract a persistable conversation item from a runner SSE event.

    Returns a ``NewConversationItem`` for:

    - ``response.output_item.done`` events carrying an assistant
      message, function_call, or function_call_output.
    - ``compaction`` events carrying a conversation summary from
      the runner's compaction system.

    Returns ``None`` for all other events (transient deltas, turn
    lifecycle, compaction progress indicators, etc.).

    :param event: Parsed SSE event dict from the runner stream.
    :param response_id: Turn-scoped id from the most recent
        ``response.in_progress`` event. All items persisted within
        the same turn share this id so the web UI can group them
        into a single bubble and pair function_calls with their
        outputs. Falls back to a fresh uuid when unavailable.
    :returns: A ``NewConversationItem`` ready for
        ``conv_store.append()``, or ``None``.
    """
    import uuid

    evt_type = event.get("type")

    if evt_type == "compaction":
        try:
            data = parse_item_data("compaction", event)
        except (ValueError, TypeError):
            _logger.warning("Failed to parse compaction item from SSE")
            return None

        return NewConversationItem(
            type="compaction",
            response_id=f"compact_{uuid.uuid4().hex}",
            data=data,
        )

    if evt_type != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in ("message", "function_call", "function_call_output"):
        return None
    # Skip transient observed function_call events (status
    # ``in_progress`` / ``action_required``).  Only ``completed``
    # function_calls are durable — the scaffold emits them after
    # the dispatch Future resolves.  Persisting interim statuses
    # creates orphan conversation items whose spinners never
    # resolve in the web UI.
    if item_type == "function_call" and item.get("status") != "completed":
        return None
    try:
        data = parse_item_data(item_type, item)
    except (ValueError, TypeError):
        _logger.warning(
            "Failed to parse persistent item from SSE: %s",
            item_type,
        )
        return None

    return NewConversationItem(
        type=item_type,
        response_id=response_id or f"turn_{uuid.uuid4().hex}",
        data=data,
    )

def _error_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Build a durable ``error`` item from a runner error SSE event.

    The web UI already renders live ``response.error`` and
    ``response.failed`` error payloads as real error banners. This
    helper mirrors turn-scoped payloads into conversation history so the
    banner survives refresh/reconnect.

    A bare ``response.error`` emitted before ``response.in_progress`` is
    a session/startup signal, not a transcript turn. Leaving it live-only
    avoids creating an orphan banner at the top of the transcript; when
    a user sends a message into the failed native terminal, the AP-side
    fast-fail path records that user item and its sibling error in order.

    :param event: Parsed runner SSE event.
    :param response_id: Current response id, e.g. ``"resp_abc123"``.
        ``None`` means no turn is active.
    :returns: A ``type="error"`` item, or ``None`` when the event has
        no structured error payload or is not tied to a turn.
    """
    evt_type = event.get("type")
    raw_error: Any
    source = event.get("source")
    if evt_type == "response.error":
        if response_id is None:
            return None
        raw_error = event.get("error")
    elif evt_type == "response.failed":
        raw_response = event.get("response")
        raw_error = raw_response.get("error") if isinstance(raw_response, dict) else None
        if raw_error is None:
            raw_error = event.get("error")
        source = "execution"
        if response_id is None and isinstance(raw_response, dict):
            raw_response_id = raw_response.get("id")
            if isinstance(raw_response_id, str) and raw_response_id:
                response_id = raw_response_id
    else:
        return None
    if response_id is None:
        return None
    if not isinstance(raw_error, dict):
        return None
    raw_code = raw_error.get("code")
    raw_message = raw_error.get("message")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None
    if not isinstance(raw_message, str) or not raw_message.strip():
        return None
    if source not in ("llm", "execution", "tool"):
        return None
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source=source,
            code=raw_code,
            message=raw_message,
        ),
    )

async def _rescue_compaction_to_memory(
    conversation_store: ConversationStore,
    session_id: str,
    persisted_items: list[Any],
) -> None:
    """Rescue a just-persisted compaction summary into the agent's episodic memory.

    D6 (BDP-2276, ADR-0132/0142): when a compaction summary is persisted, copy it
    into durable long-term memory so distilled session knowledge survives the
    lossy compaction boundary — the substrate for "agents remember yesterday".

    Best-effort and cheap on the hot path: returns immediately unless a
    compaction item was actually just persisted, and only then resolves the
    conversation's bound agent (the memory owner) and writes the summary. A
    capture failure never blocks or surfaces — the durable conversation item
    stays the source of truth; the memory is a derived, recallable copy.

    :param conversation_store: The store the item was persisted through (used to
        resolve the conversation's owning agent).
    :param session_id: The conversation whose item was just persisted.
    :param persisted_items: The store-assigned items returned by ``append``.
    """
    if not any(getattr(it, "type", None) == "compaction" for it in persisted_items):
        return
    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        agent_id = conv.agent_id if conv is not None else None

        from omnigent.runtime import get_memory_store
        from omnigent.runtime.memory_capture import capture_compaction_summaries

        await asyncio.to_thread(
            capture_compaction_summaries,
            get_memory_store(),
            session_id,
            agent_id,
            persisted_items,
        )
    except Exception:
        _logger.exception(
            "episodic compaction capture failed for session=%s",
            session_id,
        )

def _is_child_user_message_event(conv: Conversation, body: SessionEventInput) -> bool:
    """
    Return whether *body* is a user message posted into a child session.

    Parent sessions keep the relay-before-forward guard so a fast reply cannot
    complete before the server is listening. Child-session sends originate from
    an already-running parent turn: blocking the first child message on a child
    SSE relay can deadlock delegation when no UI has subscribed to that child.
    """
    return (
        conv.parent_conversation_id is not None
        and body.type == "message"
        and body.data.get("role") == "user"
    )

async def _run_compact_locked(
    session_id: str,
    conv: Conversation,
    agent_store: AgentStore,
    agent_cache: AgentCache | None,
) -> None:
    """
    Run explicit compaction while holding the per-session compact lock.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row.
    :param agent_store: Agent store for spec lookup.
    :param agent_cache: Agent cache for bundle loading.
    """
    if conv.agent_id is None:
        raise OmnigentError("Session has no agent binding", code=ErrorCode.INTERNAL_ERROR)
    if agent_cache is None:
        raise OmnigentError(
            "Compaction is unavailable: agent cache is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Check live status via cache; tasks table has been removed.
    if _session_status_cache.get(session_id) in ("running", "waiting"):
        raise OmnigentError(
            "Cannot compact while a turn is running; cancel or wait for it to finish first",
            code=ErrorCode.CONFLICT,
        )
    agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
    if agent is None or agent.bundle_location is None:
        raise OmnigentError(
            f"Agent not found: {conv.agent_id!r}",
            code=ErrorCode.NOT_FOUND,
        )
    loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
    spec = loaded.spec
    if spec.llm is not None:
        llm_config = spec.llm
    elif spec.executor.model is not None:
        from omnigent.spec.types import LLMConfig

        llm_config = LLMConfig(model=spec.executor.model, connection=spec.executor.connection)
    else:
        raise OmnigentError(
            "Compaction requires a configured LLM model",
            code=ErrorCode.INVALID_INPUT,
        )
    task_id = f"compact_{int(time.time() * 1000)}"
    _publish_status(session_id, "running")
    # compact() publishes its own in_progress / completed SSE events
    # when conversation_id is set — don't double-publish here.
    from omnigent.runtime.workflow import compact_conversation_now

    try:
        await compact_conversation_now(
            task_id=task_id,
            conversation_id=session_id,
            spec=spec,
            llm_config=llm_config,
            tool_schemas=[],
            preserve_recent_window=1,
        )
    except Exception as exc:
        _logger.exception("Explicit session compaction failed for %s", session_id)
        detail = str(exc) or repr(exc)
        _publish_compaction_failed(session_id)
        _publish_status(session_id, "idle")
        raise OmnigentError(
            f"Compaction failed while generating a summary: {detail}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    _publish_status(session_id, "idle")

def _agent_provider_family(agent: Agent) -> str | None:
    """Return the provider family of an agent's harness, or ``None``.

    Loads the agent's spec to read its ``harness_kind`` and maps it to a
    provider family (``"anthropic"`` / ``"openai"``). Returns ``None`` when
    the bundle can't be loaded or the harness is unknown — callers treat
    ``None`` as "can't confirm same family".

    :param agent: The agent whose harness family to resolve.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None``.
    """
    from omnigent.onboarding.provider_config import provider_family_for_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → unknown family
        return None
    return provider_family_for_harness(spec.executor.harness_kind)

def _same_provider_family(a: Agent, b: Agent) -> bool:
    """Return whether two agents share a (known) provider family.

    ``False`` when either family is undeterminable, so a fork that can't
    confirm both agents speak the same provider resets model settings and
    skips resuming the source's native session (the runner rebuilds the
    native transcript from Omnigent items instead).

    :param a: First agent (e.g. the fork source's agent).
    :param b: Second agent (e.g. the switch target).
    :returns: ``True`` when both resolve to the same non-``None`` family.
    """
    family_a = _agent_provider_family(a)
    return family_a is not None and family_a == _agent_provider_family(b)

def _presentation_labels_for_agent(agent: Agent) -> dict[str, str]:
    """Return the Web UI presentation labels for an agent's harness.

    A native-CLI agent runs **terminal-first** (the inline terminal is the
    main view), gated on ``omnigent.ui == "terminal"`` plus the matching
    ``omnigent.wrapper`` value; an SDK agent runs as plain chat (no such
    labels). Used by the fork route so a switched clone's UI mode matches
    the TARGET harness instead of inheriting the source's — otherwise an SDK
    clone of a claude-native session renders a stale interactive terminal.

    :param agent: The agent the fork will bind.
    :returns: ``{ui: terminal, wrapper: <value>}`` for a native agent, or
        ``{}`` for an SDK agent / undeterminable family (chat mode).
    """
    native_agent = _native_coding_agent_for_agent(agent)
    return native_agent.presentation_labels if native_agent is not None else {}

def _load_agent_spec_for_session(
    conv: Conversation,
    agent_store: AgentStore,
) -> AgentSpec | None:
    # Split from _build_policy_engine_from_spec so the caller can run the
    # cheap guardrails/default-policy skip check between the two and avoid
    # paying for engine construction when no policy could fire. Both halves
    # are blocking DB/IO, so each is run under asyncio.to_thread.
    if conv.agent_id is None:
        return None
    agent = agent_store.get(conv.agent_id)
    if agent is None:
        return None
    return (
        get_agent_cache()
        .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
        .spec
    )

def _extract_user_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from a user message event body.

    Mirrors the logic in ``workflow._extract_user_text`` but
    operates on the raw ``SessionEventInput.data`` dict rather
    than a parsed ``MessageData`` object.

    :param body: The validated ``message`` event with
        ``role: "user"``.
    :returns: Joined text from ``input_text`` / ``text`` content
        blocks. Empty string if no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)

def _extract_assistant_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from an assistant message event.

    Mirrors :func:`_extract_user_text_from_event` but for
    assistant messages. Content blocks use ``"text"`` (not
    ``"input_text"``).

    :param body: The validated ``message`` event with
        ``role: "assistant"``.
    :returns: Joined text from content blocks. Empty string if
        no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)

_DENY_SENTINEL_PREFIX = "[Denied by policy: "

def _replace_text_in_message_body(
    body: SessionEventInput,
    replacement: str,
) -> SessionEventInput:
    """
    Return a copy of the message body with all text content
    blocks replaced by *replacement*.

    Used by OUTPUT policy DENY to substitute the deny sentinel
    into the persisted message while preserving non-text content
    blocks (images, etc.) and all other body fields.

    :param body: The original assistant message event.
    :param replacement: The deny sentinel text,
        e.g. ``"[Denied by policy: harmful content]"``.
    :returns: A new body with text blocks replaced.
    """
    content = body.data.get("content") or []
    new_content: list[dict[str, Any]] = []
    replaced = False
    for block in content:
        if isinstance(block, dict) and "text" in block:
            if not replaced:
                new_content.append({"type": "output_text", "text": replacement})
                replaced = True
        else:
            new_content.append(block)
    if not replaced:
        new_content.append({"type": "output_text", "text": replacement})
    new_data = {**body.data, "content": new_content}
    return type(body)(type=body.type, data=new_data)

def _parse_session_create_metadata(metadata: str) -> SessionCreateMetadata:
    """
    Parse the JSON metadata part from bundled session creation.

    :param metadata: Raw JSON string from the multipart form,
        e.g. ``{"title": "debug auth flow"}``.
    :returns: Validated :class:`SessionCreateMetadata`.
    :raises OmnigentError: If the JSON fails the request schema.
    """
    try:
        parsed = SessionCreateMetadata.model_validate_json(metadata)
        reasoning_effort = validate_effort(
            parsed.reasoning_effort,
            "session metadata",
            EFFORT_VALUES,
        )
        # Bounds-check the native-terminal args; raises ValueError
        # (wrapped below) on a malformed or oversized list.
        _validate_terminal_launch_args(parsed.terminal_launch_args)
        return parsed.model_copy(update={"reasoning_effort": reasoning_effort})
    except (ValidationError, ValueError) as exc:
        raise OmnigentError(
            f"invalid session metadata: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

def _spec_harness(spec: AgentSpec) -> str:
    """
    Return the canonical harness identifier for a resolved spec.

    :param spec: A parsed agent / sub-agent spec.
    :returns: The canonical harness id, e.g. ``"claude-native"`` or
        ``"codex-native"``; falls back to ``executor.type`` when no
        ``harness`` is declared.
    """
    from omnigent.harness_aliases import canonicalize_harness

    harness = spec.executor.config.get("harness") or spec.executor.type
    return canonicalize_harness(harness) or harness

def _spec_config_flag_enabled(spec: AgentSpec, key: str) -> bool:
    """
    Read a boolean-ish ``executor.config`` flag, tolerating string coercion.

    The spec parser stringifies every ``executor.config`` value (see
    ``omnigent/spec/parser.py`` — ``{str(k): str(v) ...}``), so a YAML
    ``yolo: true`` arrives here as the string ``"True"``. A naive
    ``bool(value)`` is wrong: ``bool("False")`` is ``True``. This compares
    against the truthy spellings explicitly so only an intentional
    ``true`` / ``True`` enables the flag.

    :param spec: A parsed sub-agent spec.
    :param key: The ``executor.config`` key to read, e.g. ``"yolo"``.
    :returns: ``True`` only when the value is the boolean ``True`` or the
        string ``"true"`` (case-insensitive); ``False`` otherwise
        (including when the key is absent).
    """
    value = spec.executor.config.get(key)
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"

def _persist_stored_session_bundle(
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    runner_id: str | None = None,
    tenant_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Persist database rows for a bundle already written to artifacts.

    :param agent_store: Store that owns the session-scoped agent definition.
    :param conversation_store: Store that owns the session row.
    :param artifact_store: Store for deleting the bundle on failure.
    :param metadata: Validated session metadata. A set
        ``parent_session_id`` creates the conversation as a
        sub-agent child of that session.
    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact key for the stored bundle.
    :param agent_description: Optional description from the spec.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``.
    :returns: Response with the new session id.
    :raises OmnigentError: If the agent insert violates integrity
        checks or the parent session no longer exists.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    conversation: Conversation | None = None
    agent_created = False
    try:
        conversation = conversation_store.create_conversation(
            agent_id=agent_id,
            title=metadata.title,
            parent_conversation_id=metadata.parent_session_id,
            runner_id=runner_id,
            kind="sub_agent" if metadata.parent_session_id else "default",
            workspace=metadata.workspace,
            terminal_launch_args=metadata.terminal_launch_args,
            tenant_id=tenant_id,
        )
        agent_store.create(
            agent_id=agent_id,
            name=agent_name,
            bundle_location=agent_bundle_location,
            description=agent_description,
            session_id=conversation.id,
        )
        agent_created = True
        if metadata.reasoning_effort is not None:
            updated = conversation_store.update_conversation(
                conversation.id,
                reasoning_effort=metadata.reasoning_effort,
            )
            if updated is None:
                raise OmnigentError(
                    f"Session {conversation.id!r} disappeared while persisting metadata",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            conversation = updated
        if metadata.labels:
            conversation_store.set_labels(conversation.id, metadata.labels)
    except ConversationNotFoundError as exc:
        # Parent was authorized by the caller but vanished (deleted)
        # before the insert transaction ran.
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise OmnigentError(
            str(exc),
            code=ErrorCode.NOT_FOUND,
        ) from exc
    except IntegrityError as exc:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        if agent_created:
            agent_store.delete(agent_id)
        if conversation is not None:
            asyncio.run(conversation_store.delete_conversation(conversation.id))
        raise OmnigentError(
            f"session write failed integrity checks: {exc.orig}",
            code=ErrorCode.ALREADY_EXISTS,
        ) from exc
    except ValueError as exc:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        if agent_created:
            agent_store.delete(agent_id)
        if conversation is not None:
            asyncio.run(conversation_store.delete_conversation(conversation.id))
        raise OmnigentError(
            f"session write failed agent store checks: {exc}",
            code=ErrorCode.ALREADY_EXISTS,
        ) from exc
    except SQLAlchemyError:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        if agent_created:
            agent_store.delete(agent_id)
        if conversation is not None:
            asyncio.run(conversation_store.delete_conversation(conversation.id))
        raise
    except Exception:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        if agent_created:
            agent_store.delete(agent_id)
        if conversation is not None:
            asyncio.run(conversation_store.delete_conversation(conversation.id))
        raise
    return CreatedSessionResponse(
        session_id=conversation.id,
        agent_id=agent_id,
        agent_name=agent_name,
    )

def _delete_stored_session_bundle_after_failure(
    artifact_store: ArtifactStore,
    agent_bundle_location: str,
) -> None:
    """
    Delete an uploaded bundle after database creation fails.

    Cleanup failures are logged but suppressed so the original
    exception remains the error seen by callers.

    :param artifact_store: Store that contains the uploaded bundle.
    :param agent_bundle_location: Artifact key to delete, e.g.
        ``"ag_abc123/a1b2c3d4"``.
    :returns: None.
    """
    try:
        artifact_store.delete(agent_bundle_location)
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure.
        _logger.warning(
            "Failed to delete uploaded session bundle %s after rollback",
            agent_bundle_location,
            exc_info=True,
        )

_CHILD_PREVIEW_LIMIT = 150

def _latest_message_preview(
    items: list[ConversationItem],
    limit_chars: int = _CHILD_PREVIEW_LIMIT,
) -> str | None:
    """
    Return a single-line text preview from newest-first message items.

    Powers the sub-agent rail row's status line so the user can see what
    the child is saying without opening it. The caller supplies a
    batched newest-first message list for one child; this function joins
    ``input_text`` / ``output_text`` blocks from the first non-meta
    message with text, collapses whitespace, and truncates to
    ``limit_chars``. Hidden meta messages carry durable runner context
    and must never be shown as user-facing previews.

    :param items: Newest-first message items for one conversation.
    :param limit_chars: Max preview length in characters,
        e.g. ``150``.
    :returns: Truncated single-line preview text, e.g.
        ``"I'll search the codebase for references…"``, or ``None``.
    """
    for item in items:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        parts: list[str] = []
        for block in item.data.content:
            block_type = block.get("type")
            text = block.get("text")
            if block_type in ("input_text", "output_text") and isinstance(text, str):
                parts.append(text)
        collapsed = " ".join(" ".join(parts).split())
        if not collapsed:
            continue
        if len(collapsed) <= limit_chars:
            return collapsed
        # Trim to one char less than the limit so the trailing ellipsis
        # keeps the field at ``limit_chars`` total.
        return collapsed[: max(0, limit_chars - 1)].rstrip() + "…"
    return None

_UI_ADDED_AGENT_TITLE_PREFIX = "ui"
