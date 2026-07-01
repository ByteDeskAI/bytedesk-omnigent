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

The reconnect contract is bounded ``Last-Event-ID`` replay plus
snapshot reconciliation: a client opens the live stream, ``GET``s the
snapshot, and deduplicates by item id any events that appear in both
places. See ``server/API.md`` for the full contract.
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
from omnigent.communications.state import should_publish_status
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


def _chat_event_projector():
    from omnigent.server.communication_composition import get_server_communication_services

    return get_server_communication_services().chat_event_projector()


def _publish_and_persist_resource_event(
    session_id: str,
    event_type: str,
    resource_id: str,
    resource_type: str,
    conversation_store: ConversationStore,
    resource: dict[str, Any] | None = None,
) -> None:
    """Publish an SSE event and persist it as a conversation item.

    Emits the event on the live session stream so connected
    clients see it immediately, and appends a ``resource_event``
    conversation item so reconnecting clients discover it in the
    snapshot.

    :param session_id: Session/conversation identifier.
    :param event_type: SSE event type, e.g.
        ``"session.resource.created"``.
    :param resource_id: Opaque id of the affected resource.
    :param resource_type: Kind of resource, e.g. ``"terminal"``.
    :param conversation_store: Store for persisting the item.
    :param resource: Full resource dict for created events.
    """
    from omnigent.entities.conversation import ResourceEventData

    sse_payload: dict[str, Any] = {"type": event_type}
    if event_type == "session.resource.created":
        sse_payload["resource"] = resource or {}
    else:
        sse_payload["resource_id"] = resource_id
        sse_payload["resource_type"] = resource_type
        sse_payload["session_id"] = session_id

    session_stream.publish(session_id, sse_payload)

    item = NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=event_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )
    try:
        conversation_store.append(session_id, [item])
    except (AttributeError, TypeError, ValueError, RuntimeError):
        _logger.debug(
            "Failed to persist resource event for session=%s",
            session_id,
            exc_info=True,
        )

def _format_sse(event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    """
    Format an SSE event string for the wire.

    :param event_type: SSE event name, e.g.
        ``"response.output_text.delta"``.
    :param data: The event payload dict.
    :param event_id: Optional monotonic event id (BDP-2391). When set, an
        ``id:`` line is emitted so the browser/EventSource records it and
        resends it as ``Last-Event-ID`` on reconnect, driving server-side
        replay. ``None`` (synthetic frames — heartbeat/snapshot) omits the
        ``id:`` line so it never advances the client's cursor.
    :returns: A formatted SSE message string ending in two newlines.
    """
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_type}\ndata: {json.dumps(data)}\n\n"

def _resilient_stream_payload(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    """
    Validate a session-stream event for the wire, resiliently (BDP-2399).

    A single malformed event must NEVER crash the whole session SSE stream.
    The canonical offender is a runner ``response.failed`` that omits the
    schema-required ``response`` field (e.g. a turn that failed with
    ``status: 204``): strict validation raises ``ValidationError`` which, left
    to propagate out of :func:`_stream_live_events`'s async generator, kills the
    entire stream's TaskGroup — silently hiding the error and every subsequent
    event for that session.

    Instead we **expose the error on both sides and never skip it**: log it
    loudly here (the producer side) and return the *raw* event so the client
    still receives it (the consumer side, which renders ``response.failed`` as a
    real error banner). Validation never swallows an event — a valid event is
    returned model-normalized, a malformed one is forwarded verbatim.

    :param event: The raw stream event dict (already known to carry a string
        ``type``).
    :param session_id: Owning session id, for the diagnostic log line.
    :returns: The validated+normalized payload, or the raw event on a schema
        mismatch.
    """
    try:
        return _SERVER_STREAM_EVENT_ADAPTER.validate_python(event).model_dump()
    except ValidationError as exc:
        _logger.error(
            "session %s stream: %s event failed schema validation; forwarding raw "
            "so the error is exposed, not hidden: %s",
            session_id,
            event.get("type"),
            exc,
        )
        return event

def _parse_last_event_id(request: Request) -> int | None:
    """
    Read the SSE resume cursor (BDP-2391) from a stream request.

    Prefers the standard ``Last-Event-ID`` header (sent automatically by a
    browser ``EventSource`` on reconnect); falls back to a ``last_event_id``
    query param for non-EventSource consumers. Returns ``None`` for a fresh
    connect or a non-integer value (treated as no resume).

    :param request: The FastAPI stream request.
    :returns: The integer cursor, or ``None``.
    """
    raw = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

@dataclass
class SessionLiveness:
    """
    The two honest liveness signals for a single session.

    Returned (keyed by session id) by the server's
    ``_bulk_session_liveness`` / ``_session_liveness`` lookups and
    consumed by the list-item builder, the ``WS /v1/sessions/updates``
    stream, the single-session ``SessionResponse`` snapshot, and
    ``GET /health``. Splitting the old single conflated boolean into
    two fields lets the open-session view distinguish "runner stopped
    but host can relaunch — just send a message" from "host offline —
    reconnect / fork".

    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session. This
        is the sole reachability signal: it does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``). A session with no runner
        binding (in-process executor / not yet dispatched) reads
        ``True``.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within ``HOST_LIVENESS_TTL_S``).
        ``True`` when the session's ``host_id`` is in the online-hosts
        set, ``False`` when a ``host_id`` is set but not online, and
        ``None`` when the session has no ``host_id`` (CLI / local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False``; never participates in the
        reachability decision.
    """

    runner_online: bool
    host_online: bool | None

def _publish_subtree_cost_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
) -> None:
    """
    Re-publish each ancestor's subtree-summed cost after a child usage update.

    A sub-agent's spend is persisted on its own child conversation, so an
    ancestor's stored ``session_usage`` doesn't move when the child spends —
    yet the ancestor's displayed "Session cost" reads its own number, so a
    parent's badge would never reflect a running sub-agent. (The policy gate
    already reads the subtree sum via :func:`load_session_usage`; this is the
    display side.) For each ancestor of *session_id*, recompute its subtree
    priced cost and publish a ``session.usage`` event carrying it.

    Sync (does store reads + SSE fan-out); call via
    :func:`asyncio.to_thread`, mirroring the elicitation ancestor-publish
    helpers. ``session_stream.publish`` is safe to call from a worker thread.

    :param conv_store: Store used to discover ancestors and sum each
        ancestor's subtree usage.
    :param session_id: The child session whose usage just changed, e.g.
        ``"conv_child123"``.
    :returns: None.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        ancestor_usage = load_session_usage(ancestor_id, conv_store)
        subtree_cost = _priced_cost_for_display(ancestor_usage)
        usage_by_model = _usage_by_model_for_display(ancestor_usage)
        if subtree_cost is None and usage_by_model is None:
            # Ancestor's subtree has no priced cost or token usage yet —
            # leave its badge showing "—"/its snapshot value rather than
            # emit $0.00.
            continue
        payload: dict[str, Any] = {
            "type": "session.usage",
            "conversation_id": ancestor_id,
        }
        if subtree_cost is not None:
            payload["total_cost_usd"] = subtree_cost
        if usage_by_model is not None:
            payload["usage_by_model"] = usage_by_model
        event = SessionUsageEvent(**payload)
        session_stream.publish(ancestor_id, event.model_dump(exclude_none=True))

def _publish_input_consumed(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Publish a ``session.input.consumed`` event for a just-persisted
    conversation item.

    Mirrors the wire shape consumers depend on for rendering the
    input (user message bubble, tool-result block, etc.) at the
    moment of acceptance.

    :param session_id: The session/conversation identifier whose
        stream should receive the event.
    :param item: The persisted :class:`ConversationItem` carrying
        the canonical ``id`` / ``type`` / ``data`` fields.
    :param cleared_pending_id: When this message drained a
        :mod:`omnigent.runtime.pending_inputs` entry (native-terminal
        web message mirrored back from the transcript), that entry's
        id, e.g. ``"pending_a1b2c3"`` — so clients drop the optimistic
        bubble by id. ``None`` when nothing was drained.
    """
    _chat_event_projector().publish_input_consumed(
        session_id,
        item,
        cleared_pending_id=cleared_pending_id,
    )

def _publish_compaction_in_progress(session_id: str) -> None:
    """
    Publish the standard compaction progress event to a session stream.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    _chat_event_projector().publish_compaction_in_progress(session_id)

def _publish_compaction_completed(session_id: str, total_tokens: int | None) -> None:
    """
    Publish the compaction-finished event to a session stream.

    Emitted after :func:`compact_conversation_now` returns
    successfully. Clients that rendered a spinner on the
    ``response.compaction.in_progress`` event should upgrade it to
    the permanent "Conversation compacted" marker on this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        context size, e.g. ``8421``. ``None`` when unavailable.
    """
    _chat_event_projector().publish_compaction_completed(session_id, total_tokens)

def _publish_compaction_failed(session_id: str) -> None:
    """
    Publish the compaction-failed event to a session stream.

    Emitted when :func:`compact_conversation_now` raises. Clients
    that rendered a spinner on the
    ``response.compaction.in_progress`` event should dismiss it
    without leaving a permanent marker — the conversation history
    was not modified.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    _chat_event_projector().publish_compaction_failed(session_id)

def _publish_external_assistant_message(
    session_id: str,
    item: ConversationItem,
    *,
    response_id: str,
    agent_name: str,
) -> None:
    """
    Broadcast an assistant message appended outside the task runtime.

    Terminal-backed integrations such as native Claude produce output
    in a live terminal first, then mirror the semantic text into AP.
    There is no ``agent_task`` to watch, so this helper publishes the
    completed output item directly. The browser reducer renders the
    persisted message content from ``response.output_item.done``;
    emitting synthetic text deltas here would duplicate the same
    transcript item when the snapshot path also sees it.

    :param session_id: Session/conversation identifier.
    :param item: Persisted assistant message item.
    :param response_id: Legacy endpoint response id. The persisted
        item already carries this value, so the publisher does not
        need it separately.
    :param agent_name: Legacy endpoint agent/model name. The
        persisted item already carries this value.
    :returns: None.
    """
    _chat_event_projector().publish_external_assistant_message(
        session_id,
        item,
        response_id=response_id,
        agent_name=agent_name,
    )

def _publish_external_conversation_item(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Broadcast a terminal-observed conversation item.

    User messages use ``session.input.consumed`` so the web UI renders
    them exactly like local/composer messages. Assistant/tool-side
    items use ``response.output_item.done`` because they are already
    completed records from Claude's transcript, not token deltas from
    an active Omnigent task.

    :param session_id: Session/conversation identifier.
    :param item: Persisted conversation item.
    :param cleared_pending_id: For a native user message, the id of the
        optimistic pending-input entry the caller drained for it (so
        clients drop that bubble by id), or ``None``. The drain happens
        at the persist site — see :func:`_persist_external_conversation_item`
        — because it also folds the entry's file blocks into the durable
        item before append.
    :returns: None.
    """
    _chat_event_projector().publish_external_conversation_item(
        session_id,
        item,
        cleared_pending_id=cleared_pending_id,
    )

def _publish_external_output_text_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed assistant text delta.

    Terminal-backed integrations can observe streaming output before
    their completed transcript item is available. This publishes the
    standard Responses-style text-delta SSE event without persisting
    anything; the final assistant message is persisted separately when
    the integration posts ``external_conversation_item``.

    The optional ``message_id`` / ``index`` / ``final`` fields are
    carried through when present (claude-native live streaming) and
    omitted otherwise — ``exclude_none`` keeps the wire shape identical
    to in-process task streaming for callers that don't set them.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or any
        provided ``message_id`` / ``index`` / ``final`` has the wrong
        type.
    """
    _chat_event_projector().publish_external_output_text_delta(session_id, body.data)

def _publish_session_created(
    parent_id: str,
    child_session_id: str,
    agent_id: str | None,
) -> None:
    """
    Emit ``session.created`` on the parent's stream for a child session.

    Clients watching the parent (e.g. the ap-web Subagents rail tab)
    invalidate their ``child_sessions`` cache and re-fetch on this
    event.

    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param child_session_id: The minted (or adopted) child id, e.g.
        ``"conv_child456"``.
    :param agent_id: Agent id stamped on the child (the parent's
        agent), e.g. ``"ag_abc123"``. ``None`` only for legacy parents
        without one.
    """
    _chat_event_projector().publish_session_created(parent_id, child_session_id, agent_id)

def _publish_status(
    session_id: str,
    status: str,
    error: ErrorDetail | None = None,
    response_id: str | None = None,
) -> None:
    """
    Publish a typed :class:`SessionStatusEvent` to the live stream and
    update the cache the list endpoint reads.

    ``status`` must be one of the literals on
    :class:`SessionStatusEvent` (``idle`` / ``launching`` / ``running`` /
    ``waiting`` / ``failed``); other values fail Pydantic validation rather than
    silently shipping a non-conforming wire shape (rule 15).

    Every publish site funnels through here so the in-memory
    ``_session_status_cache`` stays coherent with the SSE stream.
    Without this, paths that publish but don't write the cache —
    notably the ``external_session_status`` handler used by the
    claude-native forwarder — leave the sidebar stuck on "idle"
    while the chat itself shows "Working…".

    :param session_id: Session/conversation identifier.
    :param status: New session status value.
    :param error: Failure detail to forward on a ``"failed"``
        transition, e.g. ``ErrorDetail(code="runner_error",
        message="turn setup failed: ...")``. ``None`` for every
        non-failed transition. Carrying it lets clients render a
        terminal error line for SETUP-phase failures that never emit
        a ``response.failed`` event.
    :param response_id: Optional response id for terminal-backed status
        edges, e.g. ``"codex_turn_abc123"``.
    """
    _chat_event_projector().publish_status(
        session_id,
        status,
        error=error,
        response_id=response_id,
    )

def _publish_sandbox_status(session_id: str, stage: str, error: str | None = None) -> None:
    """
    Publish a typed :class:`SessionSandboxStatusEvent` and update the
    cache the snapshot reads.

    Every stage transition of a managed-sandbox launch funnels through
    here so the in-memory ``_session_sandbox_status_cache`` stays
    coherent with the SSE stream — a client opening the session
    mid-launch seeds its progress indicator from the snapshot's
    ``sandbox_status`` field, while already-connected clients update
    live off this event. Thread-safe (``session_stream.publish`` is a
    thread-safe broadcast and the cache write is a single dict
    assignment), so the launch pipeline may call this from the worker
    thread its sandbox exec steps run on.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — one of
        :data:`omnigent.server.schemas.SandboxLaunchStage`.
    :param error: Failure detail when *stage* is ``"failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` for non-terminal stages.
    """
    # "ready" evicts: from then on the session looks like any
    # host-bound session and the snapshot carries no launch state.
    # Failures stay cached (mirroring ManagedLaunchTracker retention)
    # so a reload after a dead launch still shows the reason.
    _chat_event_projector().publish_sandbox_status(session_id, stage, error=error)

def _publish_changed_files_invalidated(session_id: str, environment_id: str = "default") -> None:
    """
    Publish a coarse filesystem-change invalidation to the live stream.

    The event tells web clients to refetch visible filesystem views
    for the environment instead of polling the tree while a session is
    active. It is intentionally coarse because git-mode workspaces can
    only answer "the working tree changed" cheaply, not per-directory
    deltas.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param environment_id: Environment resource id,
        e.g. ``"default"``.
    """
    _chat_event_projector().publish_changed_files_invalidated(
        session_id,
        environment_id=environment_id,
    )

def _publish_interrupted(session_id: str, response_id: str | None = None) -> None:
    """
    Publish a ``session.interrupted`` event to the live stream.

    The event is co-emitted with ``response.incomplete`` (reason
    ``"user_interrupt"``) by the runtime cancel handler so off-the-
    shelf Responses parsers still close cleanly. This helper is
    responsible only for the session-level signal — not the
    response-level one.

    :param session_id: The session/conversation identifier whose
        stream should receive the event, e.g. ``"conv_abc123"``.
    :param response_id: Optional response id for terminal-backed
        interrupted turns, e.g. ``"codex_turn_abc123"``.
    """
    _chat_event_projector().publish_interrupted(session_id, response_id=response_id)

def _publish_error_event(session_id: str, error: ErrorData) -> None:
    """
    Publish a live ``response.error`` event for a persisted error item.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param error: Durable error payload to mirror into SSE.
    :returns: None.
    """
    _chat_event_projector().publish_error_event(session_id, error)

async def _stream_live_events(
    request: Request,
    session_id: str,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
    viewer_user_id: str | None = None,
    viewer_idle: bool = False,
    presence_root_id: str | None = None,
    last_event_id: int | None = None,
    keepalive_runner_router: RunnerRouter | None = None,
) -> AsyncIterator[str]:
    """
    Yield SSE-formatted events from the conversation's live stream.

    Events are delivered live from the moment
    :func:`session_stream.subscribe_with_ids` is invoked forward. When the
    caller supplies ``last_event_id``, recent missed events are replayed
    before the live tail. Clients reconcile older pre-subscribe state via
    the snapshot endpoint (``GET /v1/sessions/{id}``) and dedupe by item id.

    On client disconnect the subscribe loop breaks; the
    ``finally`` block emits a ``[DONE]`` sentinel so well-behaved
    SSE consumers see a clean stream termination. The pub-sub
    layer auto-cleans this generator's subscriber slot in its own
    ``finally`` when iteration exits.

    Each emitted dict is validated against
    :data:`ServerStreamEvent` at the wire boundary so a runtime
    that publishes an unmodelled ``type`` fails loud rather than
    serializing an unknown event verbatim.

    The subscribe call passes a ``ready_event`` heartbeat plus
    ``heartbeat_interval_s``. The ready heartbeat is yielded
    immediately after the live-tail subscriber slot is registered,
    before any snapshot hook runs, so clients can wait for a
    concrete subscription acknowledgment before posting a fast
    one-shot turn. The interval heartbeat keeps an idle stream
    emitting ``session.heartbeat`` events on a fixed cadence (see
    :data:`_SESSION_STREAM_HEARTBEAT_INTERVAL_S`). Without that,
    a stream that sits between turns has nothing crossing the wire;
    the client's SSE read-timeout and this route's
    ``request.is_disconnected()`` check (only polled on event
    arrival) both lag for minutes after a half-open socket forms
    (e.g. after a laptop sleep). The heartbeat gives both sides a
    regular byte to fire against.

    :param request: The FastAPI request, used to detect disconnect.
    :param session_id: Session/conversation identifier whose stream
        to subscribe to, e.g. ``"conv_abc123"``.
    :param on_subscribed: Optional snapshot-on-connect hook forwarded to
        :func:`session_stream.subscribe`; its events are yielded ahead of
        the live tail so a fresh client sees current resource state
        without polling. ``None`` (default) keeps the pure live-tail
        shape used by callers that reconcile via the snapshot endpoint.
    :param viewer_user_id: Authenticated identity to register in the
        session's presence registry for this stream's lifetime, e.g.
        ``"alice@example.com"``. ``None`` (default, and the reserved
        single-user sentinel mapped via ``attribution_user``) skips
        presence tracking entirely.
    :param viewer_idle: The viewer's connect-time idle flag (tab
        backgrounded), from the route's ``idle`` query param. Ignored
        when *viewer_user_id* is ``None``.
    :param presence_root_id: Root conversation of the streamed
        session's tree (its ``root_conversation_id``), e.g.
        ``"conv_root123"``. Presence is scoped to the tree's root so
        viewers of different agents/sub-agents in one session see
        each other. Required when *viewer_user_id* is set; ignored
        otherwise.
    :param keepalive_runner_router: When set, hold the session's runner warm
        for this stream's lifetime by pinging it on a cadence (BDP-2601), so a
        conversation a user is viewing is not idle-reaped. ``None`` (default,
        and in-process setups) disables the keepalive.
    :returns: An async iterator of SSE message strings.
    :raises ValueError: If *viewer_user_id* is set without
        *presence_root_id* — a per-conversation presence scope would
        silently split a session's viewers per agent.
    """
    # Presence registers before the subscribe loop: the join broadcast
    # fans out to ALREADY-subscribed co-viewers, while this stream
    # learns the full list (self included) from the snapshot-on-connect
    # presence event — full-state events make that ordering race benign.
    presence_token: str | None = None
    if viewer_user_id is not None:
        if presence_root_id is None:
            raise ValueError("presence_root_id is required when viewer_user_id is set")
        presence_token = presence.connect(
            presence_root_id, session_id, viewer_user_id, viewer_idle
        )
    # Hold the runner warm while this stream is attached (BDP-2601). Bounded to
    # attached viewers (refcounted); released in ``finally`` on disconnect so an
    # abandoned session still idle-reaps. Works regardless of presence tracking
    # (which is skipped for the single-user ``local`` account).
    keepalive_active = False
    if keepalive_runner_router is not None:
        _acquire_runner_keepalive(session_id, keepalive_runner_router)
        keepalive_active = True
    try:
        async for seq, event in session_stream.subscribe_with_ids(
            session_id,
            heartbeat_interval_s=_SESSION_STREAM_HEARTBEAT_INTERVAL_S,
            ready_event={"type": "session.heartbeat"},
            # In-flight text replay must be captured synchronously at slot
            # registration (before ``ready_event`` suspends), not in the
            # async ``on_subscribed`` hook, or window deltas double-render.
            # Resource state stays in ``on_subscribed`` — it needs
            # awaits and is not dedup-sensitive.
            pre_ready_snapshot=lambda: inflight_text.snapshot_for(session_id),
            on_subscribed=on_subscribed,
            # Last-Event-ID resume (BDP-2391): replay the buffered suffix the
            # client missed during a disconnect before the live tail.
            last_event_id=last_event_id,
        ):
            if await request.is_disconnected():
                break
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise ValueError(
                    f"session stream event missing string ``type`` field: {event!r}",
                )
            yield _format_sse(
                event_type, _resilient_stream_payload(event, session_id), event_id=seq
            )
    finally:
        if keepalive_active:
            _release_runner_keepalive(session_id)
        # The non-None checks besides presence_token's are type
        # narrowing only: a minted token implies both were set above.
        if (
            presence_token is not None
            and viewer_user_id is not None
            and presence_root_id is not None
        ):
            presence.disconnect(presence_root_id, viewer_user_id, presence_token)
        yield "data: [DONE]\n\n"
