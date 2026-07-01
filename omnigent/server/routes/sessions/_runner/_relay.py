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

from ._dispatch_strategies import (
    DefaultRunnerEventDispatchStrategy,
    NativeTerminalMessageDispatchStrategy,
    SessionEventDispatchContext,
    SessionEventDispatcher,
)

_logger = logging.getLogger(__name__)


def _import_parent_bindings() -> None:
    from .. import _constants as _parent_constants
    from .. import _state as _parent_state

    g = globals()
    for _mod in (_parent_constants, _parent_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()


def _sessions_facade():
    from omnigent.server.routes import sessions

    return sessions


def _session_event_dispatcher() -> SessionEventDispatcher:
    """Build the runner-event dispatcher with route-boundary dependencies."""
    sessions = _sessions_facade()
    return SessionEventDispatcher(
        strategies=(
            NativeTerminalMessageDispatchStrategy(
                is_native_terminal_session=sessions._is_native_terminal_session,
                build_native_terminal_message_event=sessions._build_native_terminal_message_event,
                ensure_native_terminal_ready=sessions._ensure_native_terminal_ready,
                persist_native_terminal_failure=sessions._persist_native_terminal_failure,
                persist_native_policy_notice=sessions._persist_native_policy_notice,
                record_pending_input=pending_inputs.record,
                resolve_pending_input=pending_inputs.resolve,
                forward_native_terminal_message=sessions._forward_native_terminal_message,
            ),
            DefaultRunnerEventDispatchStrategy(
                forward_event=sessions._forward_event_to_runner,
            ),
        )
    )


async def _forward_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    agent_name: str | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
) -> str:
    """
    Persist a user event and forward it to the runner.

    The server persists the item to the conversation store
    (invariant I1: persist-before-forward), publishes acknowledgment
    events, then POSTs the event to the runner's
    ``POST /v1/sessions/{id}/events``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The conversation row for ``session_id``.
    :param body: The validated event input from the client.
    :param conversation_store: Store for item persistence.
    :param runner_client: HTTP client pointed at the runner.
    :param agent_name: Human-readable agent name for the
        ``model`` field on the runner body, e.g. ``"research-agent"``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary content store for
        resolving ``file_id`` references before forwarding.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint so ``proxy_stream`` knows to load
        the agent spec and initialise :class:`ProxyMcpManager` for
        this turn. ``False`` by default (agents without MCP servers).
    :param created_by: Authenticated identity of the posting actor,
        recorded on the persisted item for attribution.
    :returns: The store-assigned id of the persisted item.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        item,
        conversation_store,
    )
    # Don't publish status="running" or input.consumed here —
    # wait until after the forward to the runner succeeds.
    # Publishing early causes the REPL to start its streaming
    # timer before the turn actually starts, showing a
    # premature "working" phase.

    # Resolve file_id references (input_image, input_file) to
    # inline base64 data: URIs before forwarding. The runner and
    # harness don't have access to the server's file store — the
    # LLM endpoint needs the actual content, not an internal ID.
    forwarded_data = dict(body.data)
    if (
        file_store is not None
        and artifact_store is not None
        and "content" in forwarded_data
        and isinstance(forwarded_data["content"], list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        try:
            forwarded_data["content"] = _resolve_message_content(
                forwarded_data["content"],
                file_store,
                artifact_store,
                session_id=session_id,
            )
        except (ValueError, KeyError):
            _logger.warning(
                "File reference resolution failed for session=%s",
                session_id,
                exc_info=True,
            )

    # Flatten SessionEventInput {type, data} into the runner's
    # discriminated-union shape {type, ...data_fields}. The runner's
    # POST handler expects the harness event shape, not the
    # session-API wrapper. Include agent_id so the runner can
    # resolve the harness type and spawn environment.
    runner_body: dict[str, Any] = {
        "type": body.type,
        **forwarded_data,
        "agent_id": conv.agent_id,
        # model tags the ResponseObject for REPL rendering.
        # Use the human-readable agent name when available.
        "model": agent_name or conv.agent_id or "",
        # Signal to proxy_stream that it should initialise
        # ProxyMcpManager and fetch MCP tool schemas for this turn.
        # Only included (and only True) when the agent has MCP
        # servers — False/absent saves the runner from a no-op spec
        # load on every turn for agents without MCP servers.
        "has_mcp_servers": has_mcp_servers,
        # Id of the item just persisted for this turn. On a cold runner
        # cache the runner reloads history (which includes this item in
        # PRE-resolution form) and drops it by id, appending its own
        # resolved copy — id-based dedup, not a role/content guess.
        "persisted_item_id": persisted_items[0].id,
    }
    # Forward request-supplied client-side tool schemas so non-native
    # harnesses can emit (and tunnel) the caller's tools — the runner
    # merges these into the harness tool list (_merge_request_client_tools).
    # Without this the runner only ever sees the spec's builtin/MCP tools
    # and the model can't invoke client-side Read/Write/Glob/etc.
    if body.tools:
        runner_body["tools"] = body.tools
    # Per-event override wins; fall back to the persisted column so a
    # UI / REPL PATCH applies even when the client doesn't repeat
    # model_override on every event. ``is not None`` over ``or`` per
    # the no-invented-defaults rule.
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source.
    if conv.harness_override is not None:
        runner_body["harness_override"] = conv.harness_override
    instruction_fragments = _instruction_fragments_for_runner_event(
        agent_id=conv.agent_id,
        agent_name=agent_name,
    )
    if instruction_fragments:
        runner_body["instruction_fragments"] = instruction_fragments

    # The runner's sessions-native POST returns 202 immediately
    # and starts the turn as a background task. No streaming
    # response to drain — events flow through GET /stream.
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=10.0,
        )
        # Publish input.consumed AFTER the forward succeeds —
        # the runner has the message and will start the turn.
        _publish_input_consumed(session_id, persisted_items[0])
    except httpx.HTTPError:
        _logger.exception(
            "Forward to runner failed for session=%s; "
            "event persisted, runner picks up on reconnect.",
            session_id,
        )
        _publish_status(session_id, "idle")

    return persisted_items[0].id


def _instruction_fragments_for_runner_event(
    *,
    agent_id: str | None,
    agent_name: str | None,
) -> list[str]:
    """Resolve server-side extension instructions to forward into runner turns."""
    if not agent_id:
        return []
    from types import SimpleNamespace

    from omnigent.kernel.extensions import extension_instruction_fragments

    return extension_instruction_fragments(
        agent_id=agent_id,
        spec=SimpleNamespace(name=agent_name or agent_id),
    )


async def _dispatch_session_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent_name: str | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    runner_router: RunnerRouter | None = None,
) -> _SessionEventDispatchResult:
    """
    Forward an item-event to the runner with harness-aware dispatch.

    Callers stay harness-agnostic — the claude-native message bypass
    is encapsulated here. Two dispatch outcomes:

    * **claude-native + ``type == "message"``**: web-chat user
      messages on these sessions must NOT be persisted by the AP
      server. The Omnigent would otherwise persist an AP-side copy AND
      let the transcript forwarder mirror the same message back
      (with its own store-assigned item id), so every web-typed
      prompt would land as two items in the chat panel. We forward
      to the bound runner so the claude-native harness types the
      message into tmux; the transcript forwarder becomes the
      single writer for the conversation history. Returns a result
      with ``item_id=None`` (no AP-side persisted item) and a
      ``pending_id`` for the optimistic-bubble index entry.

    * **All other cases**: persist the item AP-side (invariant I1:
      persist-before-forward) and forward via the harness's
      ``/events`` scaffold. Returns the persisted item id and
      ``pending_id=None``.

    The single-writer invariant is the entire reason the bypass
    exists; do NOT collapse the two branches into a single forward
    that always persists. Doing so on a native session causes
    duplicate items in the chat panel as soon as the transcript
    forwarder mirrors the same prompt back.

    The pending-input entry recorded on the native path bridges the
    transcript round-trip: until the forwarder mirrors the message
    back, it lives nowhere durable, so a client that navigates away /
    rebinds would lose the optimistic bubble. The entry is replayed
    into the snapshot and drained when the message persists (see
    :mod:`omnigent.runtime.pending_inputs`). It is rolled back if the
    forward fails, so a never-delivered message leaves no ghost.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row for *session_id*.
    :param body: Validated event from the client.
    :param conversation_store: Used by the non-native path to
        persist the item.
    :param runner_client: The session's runner client, already
        resolved by the caller via :func:`_get_runner_client`.
    :param agent_name: Human-readable agent name for the
        ``model`` field on non-native forwards.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary store for the same.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint. ``False`` by default.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``. On the non-native path it is
        recorded directly on the persisted item. On the claude-native
        bypass the transcript forwarder is the single writer, so
        ``created_by`` is stored in the ``pending_inputs`` entry via
        :func:`omnigent.runtime.pending_inputs.record` and applied
        to the item when the forwarder mirrors it back (see
        :func:`_persist_external_conversation_item`).
    :param runner_router: Router used to resolve the runner for the
        native-terminal parent-wake forward when a sub-agent fails to
        boot (see :func:`_persist_native_terminal_failure`). ``None``
        in in-process / test setups where the global client is used.
    :returns: A :class:`_SessionEventDispatchResult` carrying the
        persisted item id (non-native) or the pending-input id
        (claude-native message bypass).
    """
    return await _session_event_dispatcher().dispatch(
        SessionEventDispatchContext(
            session_id=session_id,
            conversation=conv,
            body=body,
            conversation_store=conversation_store,
            runner_client=runner_client,
            agent_name=agent_name,
            file_store=file_store,
            artifact_store=artifact_store,
            has_mcp_servers=has_mcp_servers,
            created_by=created_by,
            runner_router=runner_router,
        )
    )


async def _relay_persist_error_once(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> Literal["persisted", "duplicate", "skipped", "failed"]:
    """
    Persist a runner error item unless the same error already exists.

    Native terminal startup can fail again on every runner reconnect.
    Dedupe by the visible payload ``(source, code, message)`` only
    when no user message has appeared since the matching error. That
    suppresses reconnect spam while still recording a new error for a
    user-initiated retry against the same broken terminal.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param item: The candidate ``type="error"`` item.
    :returns: ``"persisted"`` if this call appended the item,
        ``"duplicate"`` if a matching recent error already exists,
        ``"skipped"`` if no store or non-error item was provided, or
        ``"failed"`` if the store operation failed.
    """
    if conversation_store is None:
        return "skipped"
    if not isinstance(item.data, ErrorData):
        return "skipped"
    try:
        recent = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=20,
            order="desc",
        )
        for existing in recent.data:
            if (
                existing.type == "message"
                and isinstance(existing.data, MessageData)
                and existing.data.role == "user"
            ):
                break
            if existing.type != "error" or not isinstance(existing.data, ErrorData):
                continue
            if (
                existing.data.source == item.data.source
                and existing.data.code == item.data.code
                and existing.data.message == item.data.message
            ):
                return "duplicate"
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
        return "persisted"
    except Exception:
        _logger.exception(
            "Relay error persist failed for session=%s",
            session_id,
        )
        return "failed"


async def _relay_persist(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
    *,
    raw_payload: dict[str, Any] | None = None,
    event_type: str | None = None,
) -> SequencedPersistResult | None:
    """
    Persist a single conversation item from the relay.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier.
    :param item: The item to persist.
    :param raw_payload: Raw runner event payload, when available.
    :param event_type: Raw runner event type, when available.
    """
    if conversation_store is None:
        return None
    try:
        result = await persist_sequenced_item(
            conversation_store,
            session_id,
            item,
            source="runner_relay",
            event_type=event_type or item.type,
            raw_payload=raw_payload or {"type": item.type},
        )
    except Exception:
        _logger.exception(
            "Relay persist failed for session=%s",
            session_id,
        )
        return None
    persisted_items = [item for item in [result.persisted, *result.released] if item is not None]
    await _rescue_compaction_to_memory(conversation_store, session_id, persisted_items)
    return result


async def _flush_relay_text(
    conversation_store: ConversationStore | None,
    session_id: str,
    text_acc: list[str],
    response_id: str | None,
    model_id: str | None,
) -> None:
    """
    Persist buffered assistant text as a message item and clear the buffer.

    Scaffold harnesses (claude-sdk) stream text deltas with no per-message
    ``output_item.done``, so the relay buffers them. Flushing at each
    text→function_call boundary (not only at ``response.completed``) keeps
    the persisted transcript interleaved — ``[text, tool, text, tool]`` —
    instead of collapsing a turn's narration into one block after its tool
    calls (which renders tools-above-text + run-on text on reload).

    After a confirmed persist the item is also published to the live
    stream as ``response.output_item.done`` (mirroring the native path's
    :func:`_publish_external_conversation_item`). Live clients already
    rendered the text from the deltas; the publish delivers the
    store-assigned item id so they can stamp it onto the streamed block.
    Without it the rendered block stays id-less and every reconnect's
    itemId-keyed reconciliation splices the persisted copy in as a
    duplicate. Clients must dedupe this event by CONTENT, not by
    open-section state: at a mid-turn tool-call boundary the streamed
    text has already been closed/committed client-side (by the
    function_call item or interleaved reasoning) before this publish
    arrives. The web stamps the id onto the matching streamed
    ``text_done`` block in place (ap-web ``chatStore.ts``
    ``pumpStreamEvents``); the TUI consumes a byte-equal committed
    segment (``_repl.py`` ``_TurnProseTracker``).

    The buffer and the in-flight replay are cleared ONLY after the append
    is confirmed: clearing first would let a reconnect during the persist
    ``await`` see neither the (not-yet-committed) message nor the replay,
    dropping the narration — and a swallowed append failure would lose it
    permanently. On failure the buffers are left intact so the text still
    replays and is retried at the next flush / ``response.completed``.

    :param conversation_store: Store to append to, or ``None`` to skip
        persistence (test parsing path).
    :param session_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text_acc: Accumulated delta strings; cleared in place on success.
    :param response_id: Turn id so the segment groups with its tool calls.
    :param model_id: Assistant agent label for the message.
    """
    if not text_acc:
        return
    text = "".join(text_acc)
    if not text.strip():
        # Whitespace-only: nothing worth persisting. Drop it so it neither
        # accumulates into the next segment nor replays as an empty bubble.
        text_acc.clear()
        inflight_text.reset_text(session_id)
        return
    if conversation_store is None:
        text_acc.clear()
        return
    import uuid

    try:
        item = NewConversationItem(
            type="message",
            response_id=response_id or f"turn_{uuid.uuid4().hex}",
            data=parse_item_data(
                "message",
                {
                    "type": "message",
                    "role": "assistant",
                    "agent": model_id or "unknown",
                    "content": [{"type": "output_text", "text": text}],
                },
            ),
        )
        result = await persist_sequenced_item(
            conversation_store,
            session_id,
            item,
            source="runner_relay_text",
            event_type="relay.text_segment",
            raw_payload={
                "type": "relay.text_segment",
                "response_id": item.response_id,
                "model": model_id,
                "text": text,
            },
        )
    except Exception:
        # Keep text_acc + the in-flight buffer so the narration isn't lost:
        # it still replays on reconnect and is retried at the next flush.
        _logger.exception(
            "Relay: failed to persist assistant text segment for session=%s",
            session_id,
        )
        return
    if result.persisted is None:
        return
    # Confirmed persisted — now safe to clear. Synchronous (no await before
    # the next yield), so no reconnect observes the committed message and a
    # stale replay together.
    text_acc.clear()
    inflight_text.reset_text(session_id)
    # Publish the persisted item so live clients learn its store-assigned
    # id and stamp it onto the already-rendered streamed text (see the
    # docstring). Ordered before the boundary item / terminal event the
    # caller publishes next; clients match it back to the streamed text
    # by byte-equal content, not by open-section state.
    done_event = OutputItemDoneEvent(
        type="response.output_item.done",
        item=result.persisted.to_api_dict(),
    )
    session_stream.publish(session_id, done_event.model_dump())


async def _relay_runner_stream(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    ready: asyncio.Event | None = None,
) -> None:
    """
    Subscribe to the runner's SSE stream and relay events locally.

    Long-lived background task that opens
    ``GET /v1/sessions/{id}/stream`` on the runner and publishes
    each event to the local ``session_stream`` pub-sub. Also
    updates ``_session_status_cache`` from turn lifecycle events
    and persists conversation items (assistant messages, tool
    calls) to the conversation store as they arrive.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_client: HTTP client pointed at the runner.
    :param conversation_store: Store for persisting conversation
        items extracted from the runner's SSE stream.
    :param ready: Optional event set once the runner stream emits its
        ready heartbeat, proving AP's runner-side no-replay subscriber
        slot is registered. ``None`` is accepted for direct unit tests
        that exercise relay parsing/persistence without asserting on
        startup readiness.
    """
    from omnigent.runtime import session_stream

    text_acc: list[str] = []
    current_response_id: str | None = None
    # Model/agent label from the turn header, stamped on text segments
    # flushed at tool-call boundaries (the boundary event carries no model).
    current_model: str | None = None
    # Map tool call_id → response_id so a function_call_output that
    # arrives after a new response.in_progress (different response_id)
    # still pairs with its matching function_call. Without this, the
    # web UI's block stream clears its pending-tool state on the
    # response_id transition and the tool card spinner never resolves.
    tool_call_response_ids: dict[str, str] = {}
    _logger.info("Relay: connecting to runner GET /stream for session=%s", session_id)

    # Read timeout: 3x the runner's session-stream heartbeat interval
    # (15s). Between turns the runner emits ``session.heartbeat`` every
    # 15s to keep proxies from dropping the idle connection. If 3
    # consecutive heartbeats are missed (45s), the connection is likely
    # dead — let the relay exit so ``_ensure_runner_relay`` can restart
    # it on the next ``POST /events``. ``connect`` stays at httpx's
    # default (5s); ``write``/``pool`` are not rate-limiting here.
    _relay_timeout = httpx.Timeout(connect=5.0, read=45.0, write=None, pool=None)
    try:
        async with runner_client.stream(
            "GET",
            f"/v1/sessions/{session_id}/stream",
            timeout=_relay_timeout,
        ) as resp:
            _logger.info("Relay: connected to runner GET /stream for session=%s", session_id)
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    data_line = next(
                        (ln for ln in frame.splitlines() if ln.startswith("data:")),
                        None,
                    )
                    if data_line is None:
                        continue
                    payload = data_line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    suppress_current_event_publish = False
                    pre_publish_items: list[ConversationItem] = []
                    post_publish_items: list[ConversationItem] = []
                    # The runner emits session.status events
                    # directly.
                    # Re-publish via _publish_status so the event
                    # gets the conversation_id field required by
                    # SessionStatusEvent's schema. The cache write
                    # happens inside _publish_status itself.
                    # Runner-emitted keepalive — consumed to reset the
                    # read timeout; not forwarded to the session stream
                    # (the Omnigent subscriber generates its own heartbeats).
                    if evt_type == "session.heartbeat":
                        if ready is not None:
                            ready.set()
                        continue

                    # Stopped turn: drop its trailing response.* output (no
                    # forward, no persist) but keep text_acc — the pre-stop
                    # narration the user watched persists at the terminal flush.
                    if session_id in _interrupt_fenced_sessions:
                        if evt_type == "session.status" and event.get("status") == "running":
                            _interrupt_fenced_sessions.discard(session_id)
                        elif evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                            # Terminal proves the stopped turn is over (completed =
                            # the stop lost the race); process it normally.
                            _interrupt_fenced_sessions.discard(session_id)
                        elif (
                            evt_type.startswith("response.")
                            and evt_type not in _FENCE_EXEMPT_EVENT_TYPES
                        ):
                            continue

                    if evt_type == "session.status":
                        status = event.get("status", "")
                        if status:
                            # Forward the runner's failure detail on a
                            # ``failed`` transition so a SETUP-phase
                            # failure (which never emits response.failed)
                            # surfaces a real error message downstream
                            # instead of ending the turn silently.
                            raw_err = event.get("error")
                            status_error = (
                                ErrorDetail.model_validate(raw_err)
                                if isinstance(raw_err, dict)
                                else None
                            )
                            if status == "failed" and status_error is not None:
                                await _persist_session_status_error_labels(
                                    session_id,
                                    status_error,
                                    conversation_store,
                                )
                            elif status == "running":
                                await _persist_session_status_error_labels(
                                    session_id,
                                    None,
                                    conversation_store,
                                )
                            # PTY-activity status is a UI signal only. Terminal
                            # sub-agent delivery rides the Stop/StopFailure hook
                            # via external_session_status (the codex-shared path)
                            # — the PTY idle oscillates on mid-turn lulls and
                            # would deliver a premature, lock-out completion.
                            _publish_status(session_id, status, status_error)
                        if status == "running":
                            text_acc.clear()
                        continue

                    # Terminal spin-up status from the runner's auto-create
                    # path. Re-publish via _publish_terminal_pending so the
                    # event carries conversation_id and the cache write
                    # (read by the snapshot) stays coherent with the stream.
                    if evt_type == "session.terminal_pending":
                        # Use ``is True`` (not bool()) so a malformed frame
                        # with a string like ``"false"`` can't strand the
                        # spinner on — the runner always sends a real bool.
                        _publish_terminal_pending(
                            session_id,
                            event.get("pending") is True,
                        )
                        continue

                    # Track the turn's response_id from lifecycle
                    # events so persisted items share one id.
                    if evt_type == "response.in_progress":
                        resp_obj = event.get("response", {})
                        _rid = resp_obj.get("id")
                        if isinstance(_rid, str) and _rid:
                            current_response_id = _rid
                        _model = resp_obj.get("model")
                        if isinstance(_model, str) and _model:
                            current_model = _model

                    # Accumulate response-scoped (scaffold) text deltas for
                    # persistence. Native message-scoped deltas (with a
                    # message_id) persist via their own output_item.done(message),
                    # so buffering them here would double-persist. Guard on
                    # non-empty str (like inflight_text.record_publish) so a
                    # malformed delta can't break the later "".join(text_acc).
                    if evt_type == "response.output_text.delta" and not event.get("message_id"):
                        _delta = event.get("delta")
                        if isinstance(_delta, str) and _delta:
                            text_acc.append(_delta)

                    # Track tool call_id → response_id so a
                    # function_call_output that arrives under a later
                    # response still pairs with its call.  Done
                    # before _extract_persistent_item_from_sse because
                    # the parse may fail (serialization alias mismatch)
                    # while the mapping is still needed for the live
                    # event patch below.
                    _raw_item = event.get("item")
                    _item = _raw_item if isinstance(_raw_item, dict) else {}
                    _item_type = _item.get("type")
                    _item_call_id = _item.get("call_id")
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and isinstance(_item_call_id, str)
                        and current_response_id is not None
                    ):
                        tool_call_response_ids[_item_call_id] = current_response_id

                    # For function_call_output, use the response_id
                    # of the matching function_call so the web UI
                    # pairs them in the same bubble even when a new
                    # response.in_progress has already overwritten
                    # current_response_id.
                    if (
                        _item_type == "function_call_output"
                        and isinstance(_item_call_id, str)
                        and _item_call_id in tool_call_response_ids
                    ):
                        _persist_rid = tool_call_response_ids[_item_call_id]
                    else:
                        _persist_rid = current_response_id

                    # Flush buffered narration as its own message BEFORE the
                    # function_call it preceded, so the transcript interleaves
                    # [text, tool, text, tool] instead of pooling a turn's text
                    # after its tool calls (tools-above-text + run-on on reload).
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and text_acc
                    ):
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            current_model,
                        )

                    conv_item = _extract_persistent_item_from_sse(
                        event,
                        response_id=_persist_rid,
                    )
                    if conv_item is not None:
                        persist_result = await _relay_persist(
                            conversation_store,
                            session_id,
                            conv_item,
                            raw_payload=event,
                            event_type=evt_type,
                        )
                        if persist_result is not None:
                            suppress_current_event_publish = persist_result.buffered
                            post_publish_items.extend(persist_result.released)

                    # On ANY terminal event (not just completed), persist the
                    # final text segment: narration streamed before a failure /
                    # cancel must survive reload too, ordered BEFORE the error
                    # item below and before the publish pops the in-flight
                    # replay entry (flush → publish keeps reload == live).
                    # NB: fenced deltas never reached text_acc (the fence's
                    # continue precedes accumulation), so a post-Stop flush
                    # carries pre-stop narration only.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _resp_obj = event.get("response")
                        _resp_model = (
                            _resp_obj.get("model") if isinstance(_resp_obj, dict) else None
                        )
                        _final_model = (
                            _resp_model
                            if isinstance(_resp_model, str) and _resp_model
                            else current_model
                        )
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            _final_model,
                        )
                        pre_publish_items.extend(
                            await flush_orphaned_outputs(conversation_store, session_id)
                        )

                    error_item = _error_item_from_sse(
                        event,
                        response_id=current_response_id,
                    )
                    if error_item is not None:
                        await _relay_persist_error_once(
                            conversation_store,
                            session_id,
                            error_item,
                        )

                    # Persist resource lifecycle events
                    # (session.resource.created / .deleted) emitted by
                    # agent-tool terminal launches/closes so reconnecting
                    # clients rediscover the resource in the snapshot.
                    # The live publish below already updates connected
                    # clients.
                    resource_item = _resource_event_item_from_sse(session_id, event)
                    if resource_item is not None:
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            resource_item,
                        )
                        # Self-heal the spin-up flag: a created terminal is
                        # authoritative proof the session is no longer
                        # "starting up", so clear it even if the runner's
                        # auto-create finally was skipped (e.g. hard kill
                        # between launch and clear). Only fire on a real
                        # state change to avoid redundant stream traffic.
                        if (
                            resource_item.data.event_type == "session.resource.created"
                            and resource_item.data.resource_type == "terminal"
                            and _session_terminal_pending_cache.get(session_id, False)
                        ):
                            _publish_terminal_pending(session_id, False)

                    # Accumulate LLM token usage from the harness
                    # response so policy callables can read
                    # event["context"]["usage"]["total_cost_usd"].
                    if evt_type == "response.completed":
                        # Persist the turn's usage (cost + token buckets) so
                        # policy callables can read
                        # event["context"]["usage"]["total_cost_usd"] and the
                        # subtree roll-up below sees the new totals.
                        _accumulate_session_usage(
                            event.get("response", {}),
                            session_id,
                            conversation_store,
                        )
                        # Push the server-computed cost AND token breakdown
                        # to the web client's session indicator, rolled up
                        # over the spawn subtree. The session's own event
                        # carries its SUBTREE total (this conversation + its
                        # sub-agents), and each ancestor gets its own subtree
                        # total on its own stream — so a supervisor's badge
                        # includes its sub-agents and a parent updates live
                        # when a relay sub-agent spends. Mirrors the native
                        # path (_persist_external_session_usage); the roll-up
                        # was wired for native only, but relay agents (e.g.
                        # claude-sdk) need it too. Cost is included only when
                        # priced; the token breakdown rides along whenever any
                        # bucket is recorded (so an unpriced session still
                        # surfaces tokens). context_tokens/window already ride
                        # on the response.completed event. Threaded: store
                        # reads + SSE fan-out.
                        _subtree_usage = await asyncio.to_thread(
                            load_session_usage,
                            session_id,
                            conversation_store,
                        )
                        _subtree_cost = _priced_cost_for_display(_subtree_usage)
                        _usage_by_model = _usage_by_model_for_display(_subtree_usage)
                        if _subtree_cost is not None or _usage_by_model is not None:
                            _usage_payload: dict[str, Any] = {
                                "type": "session.usage",
                                "conversation_id": session_id,
                            }
                            if _subtree_cost is not None:
                                _usage_payload["total_cost_usd"] = _subtree_cost
                            if _usage_by_model is not None:
                                _usage_payload["usage_by_model"] = _usage_by_model
                            session_stream.publish(
                                session_id,
                                SessionUsageEvent(**_usage_payload).model_dump(exclude_none=True),
                            )
                            await asyncio.to_thread(
                                _publish_subtree_cost_to_ancestors,
                                conversation_store,
                                session_id,
                            )

                    # Reset the turn-scoped response_id on any
                    # terminal event so it doesn't leak to the
                    # next turn.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        current_response_id = None

                    # Patch the live event's response_id for
                    # function_call_output items whose call_id maps
                    # to a known function_call response_id. This
                    # ensures the web UI's block stream pairs the
                    # tool result with its call in the same bubble.
                    if (
                        evt_type == "response.output_item.done"
                        and isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "function_call_output"
                    ):
                        _live_cid = event["item"].get("call_id")
                        if isinstance(_live_cid, str) and _live_cid in tool_call_response_ids:
                            event = {
                                **event,
                                "item": {
                                    **event["item"],
                                    "response_id": tool_call_response_ids[_live_cid],
                                },
                            }
                    if evt_type == "response.elicitation_request":
                        session_stream.publish(session_id, event)
                        await asyncio.to_thread(
                            _publish_elicitation_request_to_ancestors,
                            conversation_store,
                            session_id,
                            event,
                        )
                        continue
                    if evt_type == "response.elicitation_resolved":
                        session_stream.publish(session_id, event)
                        elicitation_id = event.get("elicitation_id")
                        if isinstance(elicitation_id, str) and elicitation_id:
                            await asyncio.to_thread(
                                _publish_elicitation_resolved_to_ancestors,
                                conversation_store,
                                session_id,
                                elicitation_id,
                            )
                        continue
                    if suppress_current_event_publish:
                        continue
                    for flushed in pre_publish_items:
                        done_event = OutputItemDoneEvent(
                            type="response.output_item.done",
                            item=flushed.to_api_dict(),
                        )
                        session_stream.publish(session_id, done_event.model_dump())
                    session_stream.publish(session_id, event)
                    for released in post_publish_items:
                        done_event = OutputItemDoneEvent(
                            type="response.output_item.done",
                            item=released.to_api_dict(),
                        )
                        session_stream.publish(session_id, done_event.model_dump())

    except (httpx.HTTPError, ConnectionError):
        # Runner transports may raise bare ConnectionError; treat it the
        # same as HTTPError so the task exits
        # gracefully instead of leaving an unretrieved exception.
        _logger.warning(
            "Relay: ended for session=%s",
            session_id,
            exc_info=True,
        )
    except asyncio.CancelledError:
        raise
    finally:
        _logger.info("Relay: task exiting for session=%s", session_id)
        # Drop any in-flight assistant-text entry so a relay that exits
        # WITHOUT a terminal turn event (runner death / tunnel drop
        # mid-turn, or a rebind cancellation) can't strand it forever.
        # Normal turn-ends already clear via record_publish.
        inflight_text.discard(session_id)
        # Relay ended (runner dropped/rebound): re-discover skills next time.
        # Cancel any in-flight fetch so it can't land stale skills from the
        # dead runner into the cache after this pop.
        _runner_skills_cache.pop(session_id, None)
        inflight = _runner_skills_inflight.pop(session_id, None)
        if inflight is not None:
            inflight.cancel()


def _ensure_runner_relay(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start (or replace) the SSE relay for ``session_id``.

    No-op when a healthy relay is already bound to ``runner_id``.
    When the bound runner changes (last-write-wins PATCH-rebind),
    the stale relay is cancelled and a fresh one is created
    against the new runner.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the new relay subscribes to,
        e.g. ``"runner_abc123"``. ``None`` skips relay
        (in-process path with no runner binding).
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay.
    :param conversation_store: Store for persisting items from
        the runner's SSE stream. ``None`` disables persistence.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    """
    if runner_client is None or runner_id is None:
        _logger.info(
            "Relay: skipping for session=%s (runner_client=%s, runner_id=%s)",
            session_id,
            runner_client is not None,
            runner_id,
        )
        return None
    existing = _runner_relay_tasks.get(session_id)
    if existing is not None:
        if existing.runner_id == runner_id and not existing.task.done():
            _logger.info("Relay: reusing existing for session=%s runner=%s", session_id, runner_id)
            return existing  # same runner, healthy task
        _logger.info(
            "Relay: replacing stale for session=%s (old_runner=%s done=%s)",
            session_id,
            existing.runner_id,
            existing.task.done(),
        )
        if not existing.task.done():
            existing.task.cancel()  # stale binding; replace
    else:
        _logger.info("Relay: creating new for session=%s runner=%s", session_id, runner_id)
    ready = asyncio.Event()
    task = asyncio.create_task(
        _sessions_facade()._relay_runner_stream(
            session_id,
            runner_client,
            conversation_store,
            ready,
        ),
        name=f"runner-relay-{session_id}",
    )
    handle = _RelayHandle(runner_id=runner_id, task=task, ready=ready)
    _runner_relay_tasks[session_id] = handle

    def _on_done(t: asyncio.Task[None]) -> None:
        # Clear our slot only if it still holds this task — a
        # later rebind may have replaced us.
        current = _runner_relay_tasks.get(session_id)
        if current is not None and current.task is t:
            _runner_relay_tasks.pop(session_id, None)

    task.add_done_callback(_on_done)
    return handle


async def _ensure_runner_relay_ready(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start the runner SSE relay and wait for its subscription ack.

    The runner stream has no replay buffer. For item events, Omnigent must
    subscribe to runner output before it forwards the input event; a
    fast harness can otherwise complete before Omnigent is listening, leaving
    the user with an apparently successful empty response.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the relay should bind to, e.g.
        ``"runner_abc123"``. ``None`` skips relay setup.
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay setup.
    :param conversation_store: Store for persisting relayed items.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    :raises OmnigentError: If the relay cannot observe the
        runner stream's ready heartbeat before the timeout.
    """
    handle = _sessions_facade()._ensure_runner_relay(
        session_id,
        runner_id,
        runner_client,
        conversation_store,
    )
    if handle is None or handle.ready.is_set():
        return handle
    try:
        await asyncio.wait_for(
            handle.ready.wait(),
            timeout=_sessions_facade()._RUNNER_RELAY_READY_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        if handle.task.done():
            raise OmnigentError(
                "Runner stream relay exited before becoming ready",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        raise OmnigentError(
            "Timed out waiting for runner stream relay to subscribe",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return handle
