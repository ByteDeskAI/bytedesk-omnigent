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


def _publish_runner_recovered_status(session_id: str) -> None:
    """
    Clear a stale failed session status after runner recovery.

    Native terminal startup failures are sticky against trailing
    ``idle`` PTY-quiescence signals so users can see the error. A
    later runner bind/session-init success is different: it proves AP
    reached a live runner for this session again, so the old failure is
    stale and should not keep the conversation marked failed until the
    next user turn emits ``running``.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    if _session_status_cache.get(session_id) != "failed":
        return
    _session_status_cache[session_id] = "idle"
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status="idle",
        error=None,
    )
    session_stream.publish(session_id, event.model_dump())

def _is_runner_unavailable_error(exc: BaseException) -> bool:
    """Uniform dead-runner detector for the read paths (BDP-2579 F2).

    Both read paths now surface a dead runner the same way: the NATS transport
    wraps unary AND stream nats errors into :class:`httpx.ConnectError`, and the
    resolve path raises ``OmnigentError(RUNNER_UNAVAILABLE)``.
    """
    if isinstance(exc, httpx.ConnectError):
        return True
    return isinstance(exc, OmnigentError) and exc.code == ErrorCode.RUNNER_UNAVAILABLE

async def _heal_session_runner(session_id: str, request: Request) -> bool:
    """Self-heal a session whose runner died (BDP-2579 rungs 1–3).

    Single-flighted: concurrent read-path heals for one session coalesce onto
    one in-flight task (the ``_run_session_heal`` work runs once); across
    replicas the cross-replica lock inside ``_run_session_heal`` coalesces.

    :returns: ``True`` when the session has a live runner again (caller
        re-resolves the client and retries); ``False`` when the heal exhausted
        every rung and left the session in the graceful ``terminal_pending``
        reconnecting state (caller must NOT 503-storm).
    """
    existing = _heal_inflight.get(session_id)
    if existing is not None and not existing.done():
        return await existing
    task = asyncio.create_task(
        _run_session_heal(session_id, request), name=f"heal-{session_id}"
    )
    _heal_inflight[session_id] = task
    try:
        return await task
    finally:
        if _heal_inflight.get(session_id) is task:
            _heal_inflight.pop(session_id, None)

async def _run_session_heal(session_id: str, request: Request) -> bool:
    """The actual heal pipeline (one runner per session at a time)."""
    from omnigent.runtime import get_runner_router

    sessions = _sessions_facade()
    cfg = load_runner_heal_config()
    app_state = request.app.state
    conversation_store: ConversationStore = app_state.conversation_store
    runner_router = getattr(app_state, "runner_router", None) or get_runner_router()
    host_registry = getattr(app_state, "host_registry", None)
    host_store = getattr(app_state, "host_store", None)
    runner_control_registry = getattr(app_state, "runner_control_registry", None)
    runner_credential_store = getattr(app_state, "runner_credential_store", None)
    runner_exit_reports = getattr(app_state, "runner_exit_reports", None)

    try:
        from omnigent.coordination.lifecycle import get_active_backplane

        backplane = get_active_backplane()
    except Exception:  # noqa: BLE001
        backplane = None

    lock_name = f"session-heal:{session_id}"
    lock_ttl = cfg.relaunch_attempt_timeout_s * cfg.relaunch_max_attempts + 30.0
    acquired = True
    if backplane is not None:
        with contextlib.suppress(Exception):
            acquired = await backplane.try_acquire(lock_name, ttl_s=lock_ttl)
    if not acquired:
        # A peer replica owns the heal — its CAS repin is the observable result.
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        client = await sessions._wait_for_runner_client(
            session_id,
            runner_router,
            runner_control_registry,
            runner_id=conv.runner_id if conv is not None else None,
            timeout_s=cfg.reconnect_hold_timeout_s,
            runner_exit_reports=runner_exit_reports,
        )
        return client is not None

    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or conv.runner_id is None or conv.host_id is None:
            return False  # nothing to heal (no host-bound runner)

        # Liveness gate: never heal a runner that is actually fine (a peer
        # replica may have just repaired it while we waited for the lock).
        existing_client = await sessions._get_runner_client(session_id, runner_router)
        if existing_client is not None and await sessions._runner_client_ready(existing_client):
            return True

        host = (
            await asyncio.to_thread(host_store.get_host, conv.host_id)
            if host_store is not None
            else None
        )

        # Managed sessions never cross-host failover (fresh sandbox = work-tree
        # loss); relaunch a new sandbox generation under the same host identity.
        if host is not None and host.sandbox_provider is not None:
            try:
                healed = await sessions._maybe_relaunch_managed_sandbox(
                    session_id=session_id,
                    conv=conv,
                    app_state=app_state,
                    conversation_store=conversation_store,
                )
            except OmnigentError:
                healed = False
            if healed:
                sessions._publish_terminal_pending(session_id, False)
                _publish_runner_recovered_status(session_id)
                return True
            sessions._publish_terminal_pending(session_id, True)
            return False

        # ── Rung 1: relaunch on the bound host (plain session) ──
        # Owner = the session's created_by (the host-pool tenancy + the runner's
        # trusted launch owner); resolved via the still-bound runner id.
        owner = await asyncio.to_thread(
            conversation_store.owner_for_runner, conv.runner_id
        )
        expected_runner = conv.runner_id
        wedged = False
        for attempt_no in range(max(cfg.relaunch_max_attempts, 1)):

            def _repin(new_rid: str, _exp: str = expected_runner) -> bool:
                return conversation_store.cas_runner_id(conv.id, _exp, new_rid)

            host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
            if host_conn is not None:
                attempt = await sessions._launch_runner_on_host(
                    conv,
                    conversation_store,
                    host_registry,
                    host_conn,
                    owner=owner,
                    runner_control_registry=runner_control_registry,
                    runner_credential_store=runner_credential_store,
                    repin=_repin,
                )
            elif host_registry is not None:
                attempt = await sessions._launch_runner_on_host_id(
                    conv,
                    conversation_store,
                    host_registry,
                    conv.host_id,
                    owner=owner,
                    runner_control_registry=runner_control_registry,
                    runner_credential_store=runner_credential_store,
                    repin=_repin,
                )
            else:
                break

            if not attempt.repinned:
                # The row moved under us (a concurrent heal / rebind won). Defer
                # to that writer's runner rather than launch a competing one.
                client = await sessions._wait_for_runner_client(
                    session_id,
                    runner_router,
                    runner_control_registry,
                    runner_id=None,
                    timeout_s=cfg.reconnect_hold_timeout_s,
                    runner_exit_reports=runner_exit_reports,
                )
                if client is not None:
                    return True
                break
            expected_runner = attempt.runner_id
            if not attempt.acked:
                # BDP-2491 wedged host: a registered tunnel that never ACKs.
                # This is the host-wedge signal for rung 2.
                wedged = True
                if host_conn is not None and host_registry is not None:
                    with contextlib.suppress(Exception):
                        host_registry.evict(host_conn)
                break
            client = await sessions._wait_for_runner_client(
                session_id,
                runner_router,
                runner_control_registry,
                runner_id=attempt.runner_id,
                timeout_s=cfg.relaunch_attempt_timeout_s,
                runner_exit_reports=runner_exit_reports,
            )
            if client is not None:
                sessions._publish_terminal_pending(session_id, False)
                _publish_runner_recovered_status(session_id)
                return True
            await asyncio.sleep(min(0.5 * (attempt_no + 1), 2.0))

        # ── Rung 2: host failover (PLAIN only, behind failover.enabled) ──
        if cfg.failover_enabled and host_store is not None:
            healed = await sessions._failover_to_new_host(
                conv=conv,
                owner=owner,
                bad_host_id=conv.host_id,
                expected_runner=expected_runner,
                wedged=wedged,
                cfg=cfg,
                conversation_store=conversation_store,
                host_store=host_store,
                host_registry=host_registry,
                runner_router=runner_router,
                runner_control_registry=runner_control_registry,
                runner_credential_store=runner_credential_store,
                runner_exit_reports=runner_exit_reports,
            )
            if healed:
                sessions._publish_terminal_pending(session_id, False)
                _publish_runner_recovered_status(session_id)
                return True

        # ── Rung 3: graceful "offline / reconnecting" (never a 503 storm) ──
        sessions._publish_terminal_pending(session_id, True)
        return False
    finally:
        if backplane is not None and acquired:
            with contextlib.suppress(Exception):
                await backplane.release(lock_name)

async def _ensure_runner_relay_ready_with_heal(
    session_id: str,
    request: Request | None,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> tuple[Conversation, httpx.AsyncClient]:
    """Start the runner SSE relay with dead-runner self-heal (BDP-2601).

    The message-send / relay-ready path was the last runner-proxy surface that
    did not self-heal: a session's stored ``runner_id`` is frequently dead by
    the time a user posts (ephemeral runners are launched on demand and
    idle/OOM-reaped), so ``_ensure_runner_relay_ready`` raised
    ``RUNNER_UNAVAILABLE`` → 503 with no recovery. This mirrors
    ``_proxy_with_runner_heal`` and the eager session-open stream heal
    (BDP-2579): when the relay handshake fails because the runner is
    unavailable and a FastAPI ``request`` is available to drive the relaunch,
    heal the session's runner, re-resolve the conversation + a fresh runner
    client for the repinned runner id, and retry the handshake ONCE. A heal
    that still can't reach a runner surfaces a clean ``RUNNER_UNAVAILABLE``
    (the API error layer maps it to 503; the heal already published the
    graceful ``terminal_pending`` reconnecting state).

    ``request`` is ``None`` only for internal callers that cannot heal (no
    FastAPI request to drive the relaunch); those keep today's behavior — one
    attempt, error propagates.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param request: The incoming FastAPI request (drives the heal), or ``None``
        to skip healing (internal callers).
    :param conv: Conversation row for *session_id*; supplies ``runner_id``.
    :param runner_client: HTTP client pointed at ``conv.runner_id`` (already
        resolved non-``None`` by the caller).
    :param conversation_store: Store used to re-resolve the conversation after
        a heal repins the runner.
    :param runner_router: Router used to re-resolve the runner client after a
        heal.
    :returns: ``(conv, runner_client)`` — refreshed to the healed runner when
        a heal occurred, otherwise the inputs unchanged.
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` when there is no request to
        heal, when the heal exhausts every rung, or when the heal cannot
        re-resolve a runner client / the retried handshake still fails.
    """
    sessions = _sessions_facade()
    try:
        await sessions._ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        return conv, runner_client
    except (OmnigentError, httpx.ConnectError) as exc:
        if request is None or not _is_runner_unavailable_error(exc):
            raise
        if not await _heal_session_runner(session_id, request):
            raise OmnigentError(
                "runner unavailable for message relay",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        refreshed = await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if refreshed is not None:
            conv = refreshed
        healed_client = await sessions._get_runner_client(session_id, runner_router)
        if healed_client is None:
            raise OmnigentError(
                "runner unavailable for message relay",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        runner_client = healed_client
        await sessions._ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        return conv, runner_client
