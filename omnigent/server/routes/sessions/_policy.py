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

def _intercept_tool(
    namespaced_name: str,
    arguments: dict[str, Any] | None,
    *,
    caller_agent_id: str | None,
    caller_department: str | None,
) -> str | None:
    """Dispatch *namespaced_name* through the extension interceptor table.

    Returns the handler's JSON result string when an extension claims the tool
    (longest matching prefix wins, so more-specific prefixes take precedence),
    or ``None`` to fall through to normal runner dispatch — both when no prefix
    matches and when the matching handler itself returns ``None``. A handler
    that raises is logged and treated as "not intercepted" so a misbehaving
    extension can never break the tool path.
    """
    table = _tool_interceptors()
    if not table:
        return None
    for prefix in sorted(table, key=len, reverse=True):
        if namespaced_name.startswith(prefix):
            try:
                return table[prefix](
                    namespaced_name,
                    arguments,
                    caller_agent_id=caller_agent_id,
                    caller_department=caller_department,
                )
            except Exception:  # noqa: BLE001 — never 500 the tool path on a bad interceptor.
                _logger.warning(
                    "tool interceptor for prefix %r failed on %r — falling through",
                    prefix,
                    namespaced_name,
                    exc_info=True,
                )
                return None
    return None

class _PendingPolicyAskWrites:
    """Policy writes deferred until a relay-path tool-call ASK is approved.

    The relay / non-native tool-call gate (:func:`_evaluate_tool_call_policy`)
    parks an ASK as a runner-owned elicitation and returns ``pending`` — it
    cannot apply the deciding policy's ``state_updates`` / ``set_labels``
    inline because the approval happens later, off that request. They are
    stashed here keyed by elicitation id and applied when the matching
    ``approval`` event resolves with ``accept`` (POLICIES.md §7.2: a denied
    ASK leaves no trace). Without this, e.g. a cost-budget soft checkpoint is
    never recorded server-side, so it re-prompts on every subsequent tool
    call. The native-harness path (:func:`_hold_native_ask_gate`) parks
    server-side and applies these inline, so it does not need this.

    :param state_updates: Deferred :class:`StateUpdate` ops to apply on
        approve, or ``None``.
    :param set_labels: Deferred label writes to apply on approve, or ``None``.
    :param from_mcp: ``True`` when created by the ``/mcp`` endpoint's
        first-call ASK path. The MCP retry path applies writes
        itself, so the events handler skips write application for
        these entries to avoid double-applying non-idempotent ops
        (e.g. ``INCREMENT`` state updates for cost-budget counters).
    """

    state_updates: list[StateUpdate] | None
    set_labels: dict[str, str] | None
    from_mcp: bool = False

def _policy_notice_from_ensure_response(resp: httpx.Response) -> str | None:
    """
    Extract a non-fatal policy-disabled notice from a 2xx ensure response.

    The runner attaches ``policy_hook_disabled_reason`` (once) to its
    terminal-ensure success body when the session degraded to no policy
    enforcement. A malformed / non-JSON body is treated as "no notice"
    rather than failing the (successful) readiness probe.

    :param resp: The runner's 2xx ensure response.
    :returns: The reason string, or ``None`` when absent / unparseable.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("policy_hook_disabled_reason")
    return reason if isinstance(reason, str) and reason.strip() else None

def _build_skill_slash_command_policy_body(body: SessionEventInput) -> SessionEventInput:
    """
    Build the user-message shape used for input policy evaluation.

    Skill commands inject a hidden meta message containing the full
    skill body, but input guardrails should evaluate the text the user
    actually typed, not the skill instructions maintained by the
    server. This preserves the legacy policy surface of
    ``/<skill> <arguments>`` without making bundled skill content
    policy-sensitive.

    :param body: Validated ``slash_command`` event body with data such
        as ``{"name": "grill-me", "arguments": "review this plan"}``.
    :returns: Synthetic user ``message`` event for policy evaluation.
    :raises OmnigentError: If the slash-command payload is invalid.
    """
    skill_name, arguments = _parse_skill_slash_command(body)
    command_text = f"/{skill_name}" if not arguments else f"/{skill_name} {arguments}"
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": command_text}],
        },
    )

def _build_policy_engine_from_spec(
    spec: AgentSpec,
    session_id: str,
    conversation_store: ConversationStore,
) -> PolicyEngine:
    caps = get_caps()
    host_connection = (
        caps.policy_llm_connection_factory() if caps.policy_llm_connection_factory else None
    )
    return build_policy_engine(
        spec=spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        server_llm=caps.llm,
        host_connection=host_connection,
    )

async def _apply_pending_policy_ask_writes(
    session_id: str,
    conv: Conversation,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    data: dict[str, Any],
) -> None:
    """
    Apply (or drop) policy writes stashed for a relay tool-call ASK.

    Called when an ``approval`` verdict resolves a runner-owned policy
    elicitation (both approval entry points — the ``approval`` event and the
    resolve URL — route here via their callers). On ``accept`` the deciding
    policy's stashed ``state_updates`` / ``set_labels`` are persisted by a
    freshly built engine — exactly what the native ``_hold_native_ask_gate``
    path does inline. On any other verdict (decline / cancel / missing) they
    are dropped (POLICIES.md §7.2: a denied ASK leaves no trace). No-op when
    the elicitation has no stashed writes (the common case — most ASKs and
    all non-policy elicitations).

    :param session_id: Session id that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param conv: The session conversation, for the agent / spec lookup.
    :param conversation_store: Store the engine persists session state to.
    :param agent_store: Store for the agent spec lookup.
    :param data: The approval payload, carrying ``elicitation_id`` and the
        verdict ``action`` (e.g. ``{"elicitation_id": "elicit_x",
        "action": "accept"}``).
    :returns: None.
    """
    elicitation_id = data.get("elicitation_id", "")
    pending = _pending_policy_ask_writes.get(elicitation_id)
    if pending is None:
        return
    if data.get("action") != "accept":
        # Declined — remove the stashed writes (POLICIES.md §7.2:
        # a denied ASK leaves no trace).
        _pending_policy_ask_writes.pop(elicitation_id, None)
        return
    if pending.from_mcp:
        # MCP entries: the retry path (POST /mcp with requestState)
        # pops and applies the writes itself. Applying here too would
        # double-apply non-idempotent ops (e.g. INCREMENT state
        # updates for cost-budget counters). Leave the entry for the
        # retry path; it owns cleanup.
        return
    # Non-MCP relay path: pop and apply writes here since no retry
    # will arrive.
    _pending_policy_ask_writes.pop(elicitation_id, None)
    # Resolve the agent spec + build the engine off the event loop: the
    # lookup, cold-cache bundle fetch, and engine construction are all
    # blocking DB/IO.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    # The label/state writes hit the DB synchronously too — keep them
    # off the loop.
    if pending.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, pending.set_labels)
    if pending.state_updates:
        await asyncio.to_thread(engine.apply_state_updates, pending.state_updates)

def _build_actor(user_id: str | None) -> dict[str, str] | None:
    """
    Build the ``actor`` dict for :class:`EvaluationContext`.

    Returns ``{"run_as": user_id}`` when the authenticated user is
    known, ``None`` otherwise (tests, legacy callers without auth).

    :param user_id: Authenticated user email from the request,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled or the caller is unauthenticated.
    :returns: Actor dict or ``None``.
    """
    if user_id is None:
        return None
    return {"run_as": user_id}

def _build_evaluation_context(
    phase: Phase,
    data: dict[str, Any],
    event: dict[str, Any],
    *,
    actor: dict[str, str] | None = None,
) -> EvaluationContext:
    """
    Build an :class:`EvaluationContext` from a proto-style event dict.

    Maps the proto ``Event.data`` shape to the internal convention:

    - ``TOOL_CALL``: ``content = {"name": name, "arguments": args}``,
      ``tool_name = name``.
    - ``TOOL_RESULT``: ``content = {"result": result_str}``,
      ``tool_name`` from ``request_data.name``,
      ``request_data`` from the event's ``request_data`` field.
    - ``REQUEST`` / ``RESPONSE``: ``content = str(data)``.

    :param phase: Internal phase enum.
    :param data: ``event.data`` dict from the proto request.
    :param event: Full event dict (for ``request_data``, ``context``).
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: Ready-to-evaluate context.
    """
    # A native hook may stamp the session's live model into the event context
    # (e.g. the codex hook reads it from ``config.toml`` at gate time — the
    # source of truth for an in-TUI ``/model`` selection). When present, this
    # wins over the engine's server-resolved model (see
    # ``PolicyEngine._inject_model``); ``None`` falls back to that resolution.
    raw_context = event.get("context") or {}
    supplied_model = raw_context.get("model")
    hook_model = supplied_model if isinstance(supplied_model, str) and supplied_model else None
    # The harness, when a native hook stamped it (e.g. the codex hook), so
    # policies can tailor messages to the session's model-switch surface
    # (codex-native is terminal-only). Carried through unchanged — the engine
    # neither resolves nor overrides it.
    supplied_harness = raw_context.get("harness")
    hook_harness = (
        supplied_harness if isinstance(supplied_harness, str) and supplied_harness else None
    )
    if phase == Phase.TOOL_CALL:
        tool_name = data.get("name") or ""
        args = data.get("arguments") or {}
        return EvaluationContext(
            phase=phase,
            content={"name": tool_name, "arguments": args},
            tool_name=tool_name or None,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    if phase == Phase.TOOL_RESULT:
        tool_result = data.get("result", "")
        request_data = event.get("request_data")
        tool_name = None
        if isinstance(request_data, dict):
            tool_name = request_data.get("name")
        return EvaluationContext(
            phase=phase,
            content={
                "result": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
            },
            tool_name=tool_name,
            request_data=request_data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # LLM_REQUEST / LLM_RESPONSE — content is the full request/response dict.
    if phase in (Phase.LLM_REQUEST, Phase.LLM_RESPONSE):
        return EvaluationContext(
            phase=phase,
            content=data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # REQUEST / RESPONSE — content is the user/assistant text.
    text = data.get("text") or data.get("content") or str(data)
    return EvaluationContext(
        phase=phase,
        content=text if isinstance(text, str) else json.dumps(text),
        actor=actor,
        model=hook_model,
        harness=hook_harness,
    )

async def _evaluate_tool_call_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a tool call against TOOL_CALL phase policy rules.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. Returns a verdict dict on DENY or ASK.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``function_call`` event with
        ``evaluate_policy: true``.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        on DENY/ASK.
    """

    tool_name = body.data.get("name")
    if not tool_name or not isinstance(tool_name, str):
        raise OmnigentError(
            "function_call event with evaluate_policy requires a non-empty 'name' field in data",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments_str = body.data.get("arguments", "{}")

    # Resolve agent spec + build engine off the event loop (blocking
    # DB/IO). Tool-call policy always evaluates (no guardrails skip).
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )

    try:
        args_payload = json.loads(arguments_str)
    except (ValueError, TypeError):
        args_payload = arguments_str

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": tool_name, "arguments": args_payload},
        tool_name=tool_name,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — publish elicitation event. Approval state lives
    # on the runner (_pending_approvals dict).
    elicitation_id = await _register_policy_elicitation(
        session_id=session_id,
        result=result,
        arguments_preview=arguments_str,
        conversation_store=conversation_store,
    )
    # The deciding policy's writes (e.g. a cost-budget checkpoint via
    # ``state_updates``) must land ONLY on approve. This relay path returns
    # ``pending`` and the verdict arrives later off-request, so stash them to
    # apply when the matching ``approval`` resolves with accept (see
    # _apply_pending_policy_ask_writes). The native path applies these inline
    # in _hold_native_ask_gate; without this, a relay/non-native session's
    # checkpoint is never recorded and the ASK re-prompts every tool call.
    # Always store an entry even when there are no deferred writes —
    # the MCP retry path checks the pending map to verify the
    # elicitation was genuinely issued by the server.
    _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
        state_updates=result.state_updates,
        set_labels=result.set_labels,
    )
    return {
        "verdict": "pending",
        "elicitation_id": elicitation_id,
        # Spec-resolved approval window; the runner's park honors it.
        "ask_timeout": resolve_ask_timeout(engine, result),
    }

def _publish_policy_deny(session_id: str, reason: str) -> None:
    """
    Publish the ``[Denied by policy: ...]`` sentinel on the session stream.

    The sentinel text is a load-bearing contract (the REPL renders it, e2e
    tests assert it, and native harnesses relay it to the model), so it is
    always carried in a ``response.output_text.delta``.

    The deny is never persisted as a conversation item — the input gate
    publishes it and returns without forwarding — so a ``message_id``-less
    delta lands in the web reducer's response-scoped text path as an
    un-reconciled "stray bubble" with no item to dedupe against. On the next
    user submit the response switch re-finalizes that still-open text,
    rendering the deny twice (observed on both native and non-native web
    sessions). Stamping a unique ``message_id`` (matching how live streaming
    text is tagged) routes it through the web's live-preview path instead,
    where it folds into a single ``live:<id>`` block.

    Safe for the other consumers: the REPL converts any ``output_text.delta``
    to a ``TextDelta`` regardless of ``message_id``; the ``/v1/responses`` API
    surfaces the deny via input-deny synthesis (not session-stream deltas);
    and the only ``message_id``-gated accumulator (``_relay_runner_stream``)
    reads runner-relayed deltas, never this server-published one.

    :param session_id: Session/conversation identifier.
    :param reason: Human-readable deny reason from the policy verdict.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": f"[Denied by policy: {reason}]",
            # Unique per deny so two separate denials don't fold into one
            # block; a single delta carries the whole sentinel, so index 0.
            "message_id": f"deny_{secrets.token_hex(8)}",
            "index": 0,
        },
    )

async def _evaluate_input_policy(
    request: Request,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a user message against REQUEST (input) phase policy rules.

    Does not persist the event. On ALLOW returns ``None`` (caller
    forwards the message). On DENY returns a verdict dict (caller does
    NOT forward). On ASK this function **parks for human approval**
    before returning: unlike the ``tool_call`` phase — where the runner
    parks via ``wait_for_user_approval`` — the REQUEST phase has no
    runner in the loop yet (the message hasn't been forwarded), so the
    approval gate must live here. It reuses :func:`_hold_native_ask_gate`
    (the same server-side park the native ``tool_call`` gate uses):
    accept collapses to ALLOW (``None``, forward the message), while
    decline / timeout collapses to a DENY verdict (fail-closed).

    :param request: The active FastAPI request, threaded to
        :func:`_hold_native_ask_gate` for upstream-disconnect detection
        while parked on an ASK.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param _runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW or an approved ASK (fall through to the
        forward path). A verdict dict ``{"verdict": "deny", "reason":
        ...}`` on DENY or a declined / timed-out ASK.
    """

    user_text = _extract_user_text_from_event(body)
    if not user_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    # Skip only when there are no agent guardrails AND no server-wide
    # default policies AND no session policies. Without this, default/
    # session policies (e.g. deny_pii_in_llm_request added via the UI)
    # are silently skipped for agents without a guardrails: YAML block.
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content=user_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — park server-side for human approval. The REQUEST phase has no
    # runner-side approval round-trip (the message has not been forwarded to
    # a runner yet, so nothing would park on a "pending" verdict — it would
    # collapse to a silent deny). Hold the gate here exactly like the native
    # tool_call path: _hold_native_ask_gate publishes the approval card,
    # awaits the human verdict on a server-side Future, and applies the
    # deciding policy's writes only on accept (POLICIES.md §7.2). Accept ->
    # ALLOW (fall through to forward the message); decline / timeout ->
    # DENY (fail-closed).
    approved = await _hold_native_ask_gate(
        request,
        session_id=session_id,
        phase=Phase.REQUEST,
        data=body.data,
        engine=engine,
        result=result,
        conversation_store=conversation_store,
    )
    if approved:
        return None
    return {
        "verdict": "deny",
        "reason": result.reason or "Denied by policy",
    }

async def _evaluate_output_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate an assistant message against OUTPUT phase policies.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. On DENY, returns a verdict dict with
    ``_denied_body`` — the caller should persist this modified
    body (text replaced with deny sentinel) instead of the
    original.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        with ``_denied_body`` on DENY.
    """

    assistant_text = _extract_assistant_text_from_event(body)
    if not assistant_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    ctx = EvaluationContext(
        phase=Phase.RESPONSE,
        content=assistant_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    # DENY — build the denied body with sentinel text.
    # The caller persists this modified body instead of the
    # original (Option B).
    if result.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
    reason = result.reason or "Denied by policy"
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    denied_body = _replace_text_in_message_body(body, sentinel)
    return {
        "verdict": "deny",
        "reason": reason,
        "_denied_body": denied_body,
    }

