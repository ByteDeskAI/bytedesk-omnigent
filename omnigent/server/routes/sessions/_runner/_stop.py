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

async def _stop_session_via_runner(
    session_id: str,
    runner_router: Any,
) -> bool:
    """
    Forward a ``stop_session`` request to the bound runner, surfacing
    failures to the caller instead of swallowing them.

    Unlike :func:`_forward_session_change_to_runner` (used for
    ``effort_change`` / ``model_change``, where a dropped forward is
    benign — the runner re-reads the persisted value at the next turn),
    a failed ``stop_session`` means the session is *still alive*. The
    web UI's "Stop session" action is destructive and treats a 2xx as
    success (it closes the confirmation dialog), so a swallowed failure
    would tell the user the session stopped when it did not. This
    helper therefore raises on a transport error or non-2xx runner
    response.

    Runner-client resolution mirrors the best-effort helper's fallback
    chain: prefer the per-session router binding, fall back to the
    global runner client (in-process / test setups). When neither
    resolves to a client there is no live runner bound — the session is
    not running on any runner, so the stop is a no-op success and this
    returns ``False`` without raising (the caller uses that to discard
    the turn fence it installed, since no runner means nothing else
    would ever lift it).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :returns: ``True`` if the stop was delivered to a runner (2xx),
        ``False`` if no runner client resolved (nothing forwarded).
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` (HTTP 503) if the
        runner could not be reached or reported a non-2xx — e.g. the
        claude-native tmux pane is wedged and ``kill_session`` failed.
        The web UI maps this to a visible "stop failed" state rather
        than closing the dialog as if the session stopped.
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return False
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _STOP_SESSION_TYPE},
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # Runner transports may raise bare ConnectionError.
        raise OmnigentError(
            f"Could not reach the runner to stop session {session_id!r}: {exc}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if resp.status_code >= 400:
        raise OmnigentError(
            f"Runner failed to stop session {session_id!r} "
            f"(status {resp.status_code}): {resp.text}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return True

async def _stop_session_host_runner(
    session_id: str,
    host_id: str,
    runner_id: str,
    host_registry: Any,
) -> None:
    """
    Terminate the host-launched runner backing a host-spawned session.

    "Stop session" on a host-spawned session must end the dedicated runner
    subprocess the host launched for it — there is exactly one runner per
    host-launched session (see ``POST /v1/hosts/{host_id}/runners`` and the
    host-launch branch of session create). Killing the ``claude`` tmux pane
    via :func:`_stop_session_via_runner` is not enough on its own: the
    runner stays connected, so ``GET /health`` keeps reporting
    ``runner_online: true`` for the session and the web UI never shows it as
    disconnected — new messages are accepted and hang on "working" against a
    dead pane.

    Bringing the runner's tunnel down is what flips ``runner_online`` to
    ``false``; ``_on_runner_disconnect`` then marks the session and the web
    UI renders the "Agent disconnected — click to show reconnect command"
    banner, identical to the end state a CLI-launched session reaches when
    its process exits.

    Best-effort by design: the pane is already gone before this runs, so a
    host that is offline, was replaced, or is slow to acknowledge is logged
    and swallowed rather than failing the whole Stop. In the common case —
    the host's ``omnigent host`` tunnel is open while the user drives
    the web UI — the stop is delivered and the runner exits. The runner this
    targets is read from the caller's own (owner-gated) session row, so it
    can only ever stop the runner bound to that session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param host_id: Owning host identifier from the session row, e.g.
        ``"host_a1b2c3d4..."``.
    :param runner_id: Runner bound to the session, e.g.
        ``"runner_token_abc123..."``.
    :param host_registry: The :class:`HostRegistry` tracking live host
        tunnels on this replica, or ``None`` when host support is not wired
        (in-process / test setups without a host tunnel).
    :returns: None.
    """
    if host_registry is None:
        return
    conn = host_registry.get(host_id)
    if conn is None:
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s is offline; "
            "the runner may linger online and the session will not show as "
            "disconnected",
            runner_id,
            session_id,
            host_id,
        )
        return
    from omnigent.host.frames import HostStopRunnerFrame, encode_host_frame

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    conn.pending_stops[request_id] = future
    stop_frame = encode_host_frame(
        HostStopRunnerFrame(request_id=request_id, runner_id=runner_id),
    )
    try:
        host_registry.send_text(conn, stop_frame)
    except ConnectionError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s connection was replaced",
            runner_id,
            session_id,
            host_id,
        )
        return
    try:
        result = await asyncio.wait_for(
            future,
            timeout=_STOP_RUNNER_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Host %s did not acknowledge stop of runner %s for session %s",
            host_id,
            runner_id,
            session_id,
        )
        return
    if result.get("status") == "failed":
        _logger.warning(
            "Host %s failed to stop runner %s for session %s: %s",
            host_id,
            runner_id,
            session_id,
            result.get("error"),
        )

