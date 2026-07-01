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

async def _persist_external_model_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a model switch made inside the terminal.

    Mirrors a ``/model`` change typed into a claude-native session's
    Claude Code pane (or picked via its in-TUI model picker) onto the
    Omnigent session: writes ``model_override`` so the value survives reload
    and publishes a ``session.model`` SSE event so the web picker
    updates live. Unlike the PATCH path
    (:func:`update_session`), this deliberately does NOT forward a
    ``model_change`` back to the runner — the terminal is already on
    the model, so re-injecting ``/model`` would loop.

    No-ops (no write, no event) when the observed model already equals
    the persisted ``model_override`` — the common case on the web→TUI
    round-trip where the web PATCH set the override moments earlier.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` (read at the route
        boundary); ``conv.model_override`` is the dedupe baseline.
    :param body: External model-change event body. ``data.model`` must
        be a non-empty string tier alias, e.g. ``"opus"``.
    :param conversation_store: Store used to upsert ``model_override``.
    :raises OmnigentError: If ``data.model`` is missing or not a
        non-empty string.
    """
    raw_model = body.data.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise OmnigentError(
            "external_model_change requires data.model to be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    model = raw_model.strip()
    if conv.model_override == model:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        model_override=model,
    )
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())

def _handle_external_session_todos(
    session_id: str,
    body: SessionEventInput,
) -> None:
    """
    Cache and broadcast a todo-list update from the claude-native forwarder.

    Updates the in-memory ``_session_todos_cache`` so subsequent
    ``GET /v1/sessions/{id}`` snapshot calls can populate the ``todos``
    field without a file read. Then publishes a ``session.todos`` SSE event
    so connected ap-web clients update their todo panel immediately.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param body: The ``external_session_todos`` event body. Must have
        ``data.todos`` as a list of todo dicts, e.g.
        ``[{"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing the bug"}]``.
    :raises OmnigentError: When ``data.todos`` is missing or not a list.
    """
    todos = body.data.get("todos")
    if not isinstance(todos, list):
        raise OmnigentError(
            "external_session_todos requires data.todos to be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    # Filter to well-formed items before caching so that malformed entries
    # from a buggy forwarder version don't persist in the snapshot.  The
    # same filter is applied by sse.ts on the live-event path; keeping the
    # two in sync means the snapshot and live panel always show the same set.
    valid_statuses = {"pending", "in_progress", "completed"}
    validated: list[dict[str, Any]] = [
        t
        for t in todos
        if isinstance(t, dict)
        and isinstance(t.get("content"), str)
        and t.get("status") in valid_statuses
        and isinstance(t.get("activeForm"), str)
    ]
    _session_todos_cache[session_id] = validated
    event = SessionTodosEvent(
        type="session.todos",
        conversation_id=session_id,
        todos=validated,
    )
    session_stream.publish(session_id, event.model_dump())

def _parse_external_assistant_message(
    body: SessionEventInput,
) -> tuple[str, str, str]:
    """
    Validate and unpack an external assistant-message event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_ASSISTANT_MESSAGE_TYPE`.
    :returns: ``(agent_name, text, response_id)``.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    agent_name = body.data.get("agent")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise OmnigentError(
            "external_assistant_message requires data.agent",
            code=ErrorCode.INVALID_INPUT,
        )
    text = body.data.get("text")
    if not isinstance(text, str) or not text:
        raise OmnigentError(
            "external_assistant_message requires non-empty data.text",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_assistant_message data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    return agent_name.strip(), text, response_id.strip()

async def _persist_external_assistant_message(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist and broadcast assistant text produced outside Omnigent tasks.

    The event is append-only conversation history. It intentionally
    bypasses the legacy persist path so mirroring a
    Claude terminal response does not create or steer an Omnigent
    agent task.

    :param session_id: Session/conversation identifier.
    :param body: External assistant-message event body.
    :param conversation_store: Store used to append the message.
    :returns: Store-assigned conversation item id.
    """
    agent_name, text, response_id = _parse_external_assistant_message(body)
    item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="assistant",
            agent=agent_name,
            content=[{"type": "output_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    persisted = persisted_items[0]
    _publish_external_assistant_message(
        session_id,
        persisted,
        response_id=response_id,
        agent_name=agent_name,
    )
    return persisted.id

def _parse_external_conversation_item(
    body: SessionEventInput,
) -> NewConversationItem:
    """
    Validate and unpack an external conversation-item event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_CONVERSATION_ITEM_TYPE`.
    :returns: A parsed :class:`NewConversationItem` ready to append.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    item_type = body.data.get("item_type")
    if not isinstance(item_type, str) or item_type not in ITEM_TYPE_TO_DATA_CLS:
        raise OmnigentError(
            "external_conversation_item requires known data.item_type",
            code=ErrorCode.INVALID_INPUT,
        )
    item_data = body.data.get("item_data")
    if not isinstance(item_data, dict):
        raise OmnigentError(
            "external_conversation_item requires object data.item_data",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_conversation_item data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    # NOTE: external conversation items are persisted with a random
    # primary key like any other item — there is no server-side dedup.
    # Producers (the claude-native / codex-native forwarders) are
    # responsible for not re-posting records they have already sent;
    # they no longer emit a ``source_id`` dedup key to the server.
    # Cap a native tool result so a multi-MB output isn't persisted + broadcast as one frame.
    if item_type == "function_call_output" and isinstance(item_data.get("output"), str):
        item_data = {**item_data, "output": cap_tool_output(item_data["output"])}
    try:
        data = parse_item_data(item_type, {"type": item_type, **item_data})
    except (ValueError, TypeError) as exc:
        raise OmnigentError(
            f"Invalid data payload for external item type {item_type!r}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return NewConversationItem(
        type=item_type,
        response_id=response_id.strip(),
        data=data,
    )

async def _persist_external_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint a child :class:`Conversation` row for a claude-native
    sub-agent and emit the parent's ``session.created`` SSE event.

    Claude Code spawns sub-agents internally via its Task tool and
    never POSTs to Omnigent to register them. The forwarder watches the
    parent's on-disk ``subagents/`` directory and calls this handler
    when a new ``.meta.json`` appears. We reuse the parent's
    ``agent_id`` (claude-native sub-agents don't have their own
    omnigent agent), stamp identifying labels, and publish the
    same ``session.created`` event omnigent-spawned children fire
    so the rail's ``child_sessions`` cache invalidates.

    Idempotent: a second POST with the same ``subagent_id`` returns
    the existing child's id without creating a duplicate — via the
    label lookup when the row is fully stamped, or via title-collision
    recovery when an earlier POST died between ``create_conversation``
    and ``set_labels`` (the recovery also re-stamps the labels so the
    row is healed for subsequent deliveries).

    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row — its ``agent_id`` is
        copied onto the child and its labels disambiguate
        claude-native parents from other harnesses.
    :param body: The POST event body. Required ``data`` keys:
        ``subagent_id`` (Claude-side id, e.g. ``"a5c7eff..."``),
        ``agent_type`` (e.g. ``"Explore"``), ``description``
        (free-form, used in the title), ``tool_use_id``
        (e.g. ``"toolu_..."``).
    :param conversation_store: Store used to read existing children
        (for idempotency) and create the new row.
    :returns: The child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: 400 if the payload is missing any of
        the required keys; 400 if the parent has no ``agent_id``
        (claude-native parents always carry one, so this would be
        a corrupted row).
    """
    subagent_id = body.data.get("subagent_id")
    agent_type = body.data.get("agent_type")
    description = body.data.get("description")
    tool_use_id = body.data.get("tool_use_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.subagent_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(agent_type, str) or not agent_type:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.agent_type",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(description, str):
        raise OmnigentError(
            "external_subagent_start requires data.description (string)",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.tool_use_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        # claude-native parents are always created with an agent_id
        # by ``omnigent claude`` (the synthetic Claude bundle).
        # A null agent_id here means we're being called against a
        # legacy / corrupt row — fail loud rather than silently
        # mint a child without a parent agent.
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a claude-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )

    # Idempotency: a forwarder retry with the same subagent_id must
    # resolve to the same child row, not mint a duplicate. The
    # forwarder also persists its own cursor file so this should be
    # rare, but the network is unreliable and the cursor write
    # happens after the POST.
    existing = await asyncio.to_thread(
        _find_claude_native_subagent_child,
        conversation_store,
        parent_id,
        subagent_id,
    )
    if existing is not None:
        return existing.id

    # Title format mirrors omnigent-spawned children
    # (``"{tool}:{session_name}"``) so the rail's split-on-colon
    # parser surfaces the same ``tool`` shape. The ``session_name``
    # half must be unique per parent because the conversation store
    # has a ``(parent_conversation_id, title)`` unique index — using
    # the description here would collide whenever Claude's LLM
    # passes the same agentType + description for parallel
    # sub-agents (which the Task tool does routinely). The
    # ``subagent_id`` is the only stable per-sub-agent identifier
    # in the meta file, so it goes here. The human-readable
    # description is stored as a label below for downstream surfaces
    # that want it; the rail's ``SubagentsPanel`` already hides the
    # ``session_name`` half so the user only sees ``agent_type``.
    title = f"{agent_type}:{subagent_id}"
    labels = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY: subagent_id,
        _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY: tool_use_id,
        _CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY: description,
    }

    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=agent_type,
        )
    except NameAlreadyExistsError:
        # The (parent, title) unique index fired: the row already exists
        # but the label-based idempotency lookup above missed it — either
        # a concurrent POST won the insert race, or an earlier POST died
        # after create_conversation and before set_labels, leaving an
        # unlabeled row. Without this recovery every forwarder redelivery
        # 500s on the same collision until the forwarder gives up and
        # parks the sub-agent (it then never appears in the rail). Adopt
        # the existing row and re-stamp its labels (idempotent upsert) so
        # the next delivery takes the fast label-lookup path.
        adopted = await asyncio.to_thread(
            _find_subagent_child_by_title,
            conversation_store,
            parent_id,
            title,
        )
        if adopted is None:
            raise
        await asyncio.to_thread(conversation_store.set_labels, adopted.id, labels)
        # The POST that created this orphan died before reaching the
        # ``session.created`` publish below, so live clients (the ap-web
        # Subagents rail) have never heard about the child — emit it now.
        # In the concurrent-race case the winner also published; a
        # duplicate event is a harmless extra cache invalidation.
        _publish_session_created(parent_id, adopted.id, parent_conv.agent_id)
        return adopted.id
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id

async def _persist_external_conversation_item(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
) -> str:
    """
    Persist and broadcast a conversation item produced outside AP.

    This is the transcript bridge path for native Claude. It appends
    user messages, assistant messages, tool calls, and tool results
    without starting or steering the placeholder Omnigent agent.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for title seeding.
    :param body: External item event body.
    :param conversation_store: Store used to append the item.
    :param created_by: Authenticated identity of the actor whose
        request triggered the forwarder POST, e.g.
        ``"alice@example.com"``. Used to attribute user messages typed
        directly in the native terminal (no pending-input entry exists
        for those). ``None`` in single-user / unauthenticated mode —
        no label is stamped in that case.
    :returns: Store-assigned conversation item id.
    """
    item = _parse_external_conversation_item(body)
    if item.type == "function_call_output" and isinstance(item.data, FunctionCallOutputData):
        identity = _recent_mirrored_tool_calls.get(item.data.call_id)
        if identity is not None and item.response_id != identity.response_id:
            item = item.model_copy(update={"response_id": identity.response_id})
    # A native user message round-tripping back from the transcript:
    # drain its optimistic pending-input entry (FIFO) and fold the
    # entry's file blocks (image / file) into the item BEFORE persisting.
    # The transcript is text-only, so without this the image is dropped
    # from durable history and disappears on every reload / navigation.
    cleared_pending_id: str | None = None
    if (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
    ):
        drained = pending_inputs.resolve_oldest(session_id)
        if drained is not None:
            cleared_pending_id = drained.pending_id
            item = _merge_pending_file_blocks(item, drained.content)
            # Apply the original sender's identity recorded at POST time.
            # The transcript forwarder is the single writer here and has no
            # auth context, so the persisted item would otherwise have
            # created_by=None, causing session.input.consumed to broadcast
            # without an author — the label would flash in from the optimistic
            # bubble then disappear once the committed item arrived.
            if drained.created_by is not None and item.created_by is None:
                item = item.model_copy(update={"created_by": drained.created_by})
        elif item.created_by is None and created_by is not None:
            # No pending entry — direct terminal input. Fall back to the
            # identity authenticated on the forwarder's own request.
            item = item.model_copy(update={"created_by": created_by})
    result = await persist_sequenced_item(
        conversation_store,
        session_id,
        item,
        source="external_conversation_item",
        event_type=body.type,
        raw_payload=body.model_dump(exclude_none=True),
    )
    if result.buffered:
        return result.audit.id
    persisted = result.persisted
    if persisted is None:
        return result.audit.id
    await _seed_missing_title_from_user_message(conv, item, conversation_store)
    _publish_external_conversation_item(
        session_id, persisted, cleared_pending_id=cleared_pending_id
    )
    _drive_terminal_resolved_elicitation(session_id, persisted)
    for released in result.released:
        _publish_external_conversation_item(session_id, released)
        _drive_terminal_resolved_elicitation(session_id, released)
    return persisted.id

def _require_external_status_forward(
    session_id: str,
    status: str,
    runner_result: _RunnerForwardResult | None,
) -> None:
    """
    Fail loudly when required external status forwarding does not land.

    Terminal native sub-agent completion is delivered to the parent
    runner through this forward. Dropping it would leave the parent
    waiting forever with no inbox result.

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param status: External status value, e.g. ``"idle"``.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: None.
    :raises OmnigentError: If the runner was unavailable or
        rejected the forwarded status.
    """
    if runner_result is None:
        raise OmnigentError(
            f"Could not reach runner to deliver external_session_status "
            f"{status!r} for sub-agent session {session_id!r}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if runner_result.status_code >= 400:
        detail = runner_result.body[:500]
        suffix = f": {detail}" if detail else ""
        raise OmnigentError(
            f"Runner rejected external_session_status {status!r} for "
            f"sub-agent session {session_id!r} with status "
            f"{runner_result.status_code}{suffix}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

