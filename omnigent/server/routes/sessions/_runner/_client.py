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

async def _get_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
) -> httpx.AsyncClient | None:
    """
    Get an HTTP client for the runner bound to a session.

    Uses the ``RunnerRouter`` to resolve the pinned runner. Falls
    back to the in-process runner client for test setups.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or
        ``None`` for in-process setups.
    :returns: An ``httpx.AsyncClient`` pointed at the runner,
        or ``None`` if no runner is available.
    """
    from omnigent.runtime import get_runner_client

    if runner_router is not None:
        try:
            routed = await runner_router.aclient_for_session_resources(
                session_id,
            )
            return routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s",
                session_id,
            )
            return None
    return cast("httpx.AsyncClient | None", get_runner_client())

async def _wait_for_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
    runner_control_registry: Any | None,
    *,
    runner_id: str | None,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None = None,
) -> httpx.AsyncClient | None:
    """
    Wait until a runner answers over the configured control plane.

    The NATS runner transport has no WebSocket-style "connected" callback,
    so readiness is an HTTP health probe through :func:`_get_runner_client`.
    The router re-checks the conversation's current ``runner_id`` binding
    on each attempt and preserves the existing ownership checks.

    When ``runner_exit_reports`` is supplied, the wait also ends the
    moment the daemon reports this runner died (``host.runner_exited``).
    That report is the authoritative "this runner is busted" signal — a
    crashed runner can never connect, so waiting out ``timeout_s`` would
    only delay the caller's failure handling. Returning ``None`` on the
    report (same as a timeout) lets the caller persist the failure the
    instant we are convinced, neither speculatively early nor a full
    timeout late.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or ``None`` for
        in-process test setups.
    :param runner_control_registry: Deprecated runner-tunnel registry slot. Accepted
        for caller compatibility; NATS readiness does not use it.
    :param runner_id: Runner id expected to connect, e.g.
        ``"runner_0123456789abcdef"``.
    :param timeout_s: Maximum seconds to wait, e.g. ``3.0``.
    :param runner_exit_reports: Crash-report store consulted to abort the
        wait early when this runner is reported dead. ``None`` keeps the
        plain wait-to-timeout behavior.
    :returns: A runner HTTP client if one becomes available, otherwise
        ``None`` (timed out, or the runner was reported dead).
    """
    if runner_id is None:
        return None
    del runner_control_registry
    if runner_router is None:
        return await _get_runner_client(session_id, runner_router)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        if runner_exit_reports is not None and runner_exit_reports.get(runner_id) is not None:
            return None
        client = await _get_runner_client(session_id, runner_router)
        if client is not None and await _runner_client_ready(client):
            return client
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(_RUNNER_CONVICTION_POLL_S, remaining))

async def _runner_client_ready(client: httpx.AsyncClient) -> bool:
    try:
        response = await client.get("/health", timeout=_RUNNER_CONVICTION_POLL_S)
    except (httpx.HTTPError, OSError, RuntimeError):
        return False
    return response.status_code == 200

async def _get_runner_client_for_resource_access(
    session_id: str,
) -> httpx.AsyncClient | None:
    """Return the authoritative runner client for session resources.

    Requires the session to be bound to a runner via
    ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise. If no
    runner router is configured (unit-test/in-process setups), callers
    may fall back to local registries.
    """
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_router = get_runner_router()
    if runner_router is not None:
        aroute = getattr(runner_router, "aclient_for_session_resources", None)
        if aroute is not None:
            routed_runner = await aroute(session_id)
        else:
            routed_runner = await asyncio.to_thread(
                runner_router.client_for_session_resources,
                session_id,
            )
        return routed_runner.client
    return cast("httpx.AsyncClient | None", get_runner_client())

def set_server_runner_router(runner_router: RunnerRouter | None) -> None:
    """
    Stash the runner router for the native-terminal approval popup.

    Called once from ``create_app`` so the tool-policy ASK gate
    (:func:`_spawn_native_approval_popup_forward`) can reach the bound
    runner from background contexts that do not carry the request / route
    closure.

    :param runner_router: The session runner router, or ``None`` in
        in-process setups.
    :returns: None.
    """
    global _server_runner_router
    _server_runner_router = runner_router

def _registered_runner_id(
    runner_router: RunnerRouter | None,
    raw_runner_id: str,
    *,
    user_id: str | None = None,
) -> str:
    """
    Validate a runner id from ``PATCH /v1/sessions/{id}``.

    When ``user_id`` is provided the function also enforces runner
    ownership: only the user who established the tunnel may
    bind sessions to that runner.

    :param runner_router: Router backed by the live tunnel registry.
        ``None`` means this server cannot bind runners.
    :param raw_runner_id: Runner id from the request body, e.g.
        ``"runner_abc123"``.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. ``None`` skips the ownership
        check (single-user / no-auth mode).
    :returns: Trimmed registered runner id.
    :raises OmnigentError: If the id is empty, the router is
        unavailable, the runner is not registered, or the caller
        does not own the runner.
    """
    runner_id = raw_runner_id.strip()
    if not runner_id:
        raise OmnigentError(
            "runner_id must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if runner_router is None:
        raise OmnigentError(
            "runner router is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if not runner_router.runner_is_online(runner_id):
        raise OmnigentError(
            f"runner {runner_id!r} is not registered",
            code=ErrorCode.INVALID_INPUT,
        )
    # Enforce runner ownership. A caller must own the runner
    # they are trying to bind to a session.
    if user_id is not None:
        runner_owner = runner_router.runner_owner(runner_id)
        if runner_owner is not None and runner_owner != user_id:
            raise OmnigentError(
                f"runner {runner_id!r} is not owned by the requesting user",
                code=ErrorCode.FORBIDDEN,
            )
    return runner_id

