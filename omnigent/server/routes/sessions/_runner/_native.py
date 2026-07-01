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


def _native_terminal_failure_from_runner_response(
    resp: httpx.Response,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert a failed runner terminal-ensure response into durable error data.

    The runner's terminal ensure endpoint must return structured
    ``{"error": {"code": ..., "message": ...}}`` for definitive startup
    failures (for example a missing native CLI). Preserve that message
    exactly so the transcript shows the real cause. If the runner returns
    an opaque framework 500 body such as ``"Internal Server Error"``,
    surface an explicit malformed-runner-response error instead of
    inventing a native terminal cause.

    :param resp: Non-2xx response from
        ``POST /v1/sessions/{id}/resources/terminals``.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_error = body.get("error")
        if isinstance(raw_error, dict):
            raw_code = raw_error.get("code")
            raw_message = raw_error.get("message")
            if (
                isinstance(raw_code, str)
                and raw_code.strip()
                and isinstance(raw_message, str)
                and raw_message.strip()
            ):
                return ErrorData(
                    source="execution",
                    code=raw_code,
                    message=raw_message,
                )
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=(
            f"Native {display_name} terminal ensure failed with malformed "
            f"runner response (HTTP {resp.status_code})."
        ),
    )

def _extract_claude_native_runner_failure(resp: httpx.Response) -> str | None:
    """
    Return a harness failure message from a runner SSE response.

    Runner ``POST /v1/sessions/{id}/events`` returns HTTP 200 for a
    syntactically valid harness stream even when the harness emits
    ``response.failed``. Claude-native Omnigent forwarding must treat that
    as failed injection, otherwise the web UI would believe a message
    reached the terminal when ``tmux send-keys`` actually failed.

    :param resp: Completed runner response.
    :returns: Failure message, or ``None`` when no failure event is
        present.
    """
    content_type = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" not in content_type and "response.failed" not in text:
        return None
    for frame in text.split("\n\n"):
        data_lines = [
            line.removeprefix("data:").strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "response.failed":
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message:
                return message
            return json.dumps(error, sort_keys=True)
        if isinstance(error, str) and error:
            return error
        return "runner reported response.failed"
    return None

async def _forward_session_change_to_runner(
    session_id: str,
    runner_router: Any,
    event: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    Best-effort POST a control event to the bound runner.

    Used for control inputs the runner dispatches by harness in its
    ``/v1/sessions/{id}/events`` handler — claude-native injects the
    corresponding slash command into the tmux pane; other harnesses
    return 204 no-op. Two kinds of caller use this:

    * PATCH-driven harness notifications (``effort_change``,
      ``model_change``) — claude-native injects the slash command,
      other harnesses re-read the persisted value at the next turn
      boundary, so they ignore the return value.
    * Explicit ``compact`` — the caller inspects the returned status
      to decide whether the runner handled the control (claude-native,
      200) or the Omnigent server must run its own in-process compaction
      (204 / no runner). See the ``compact`` branch in
      :func:`post_event`.

    Mirrors the interrupt-forward fallback chain: prefer the per-
    session router binding, fall back to the global runner client
    (in-process / test setups where the router hasn't bound the
    session). When neither resolves to a client, the POST is silently
    skipped — the persisted value on the Omnigent side is the authoritative
    fallback, picked up by the next spawn.

    Non-2xx runner responses (e.g. 503 when the tmux pane isn't
    advertised yet) are logged as warnings so the failure surfaces
    in the Omnigent log — otherwise the POST succeeds at the httpx layer
    and the status would be silently dropped.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :param event: The ``/events`` POST body, e.g.
        ``{"type": "effort_change", "effort": "high"}``,
        ``{"type": "model_change", "model": "claude-opus-4-7"}``, or
        ``{"type": "compact"}``.
    :returns: The runner's HTTP status/body, or ``None`` when no
        runner client could be resolved or the POST failed at the
        transport layer (in both cases the AP-side persisted value /
        operation is the authoritative fallback).
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _sessions_facade()._get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return None
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=5.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Session-change forward failed for session=%r type=%r",
            session_id,
            event.get("type"),
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Session-change forward rejected for session=%s type=%r status=%s body=%s",
            session_id,
            event.get("type"),
            resp.status_code,
            resp.text,
        )
    return _RunnerForwardResult(status_code=resp.status_code, body=resp.text)
