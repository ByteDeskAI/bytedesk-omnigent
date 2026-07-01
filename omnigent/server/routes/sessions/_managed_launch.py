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

_HOST_LAUNCH_RESULT_TIMEOUT_S = 15.0


def _sessions_facade():
    from omnigent.server.routes import sessions

    return sessions

@dataclass
class _HostLaunchAttempt:
    """
    Outcome of a relaunch ``host.launch_runner`` round-trip.

    :param runner_id: The token-bound runner id minted for this attempt,
        e.g. ``"runner_token_abc123..."``. Always set (the binding is
        rotated before the frame is sent), even when the host refused.
    :param error_code: Structured failure category from the host's result
        frame, e.g. ``"harness_not_configured"``; ``None`` on a successful
        launch, on a timeout waiting for the result, or when the host sent
        no code.
    :param error: Human-readable failure message from the host, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``; ``None`` when there was no error.
    :param acked: Whether the host acknowledged the launch (sent its result
        frame) within :data:`_HOST_LAUNCH_RESULT_TIMEOUT_S`. ``True`` for a
        success, a structured refusal, or a connection-replaced send — all of
        which are real host responses/decisions. ``False`` ONLY when the host
        never ACKed within the budget: its tunnel is registered but is not
        delivering ``host.launch_runner`` frames into dispatch (the wedged-host
        failure mode, BDP-2491). The relaunch caller treats ``acked=False`` as a
        host-liveness failure and evicts the dead tunnel instead of waiting out
        the full connect timeout for a runner that will never appear.
    """

    runner_id: str
    error_code: str | None = None
    error: str | None = None
    acked: bool = True
    # False only on the self-heal CAS path (BDP-2579 F3): a ``repin`` callback
    # was supplied and lost the compare-and-swap (the row moved under us — a
    # concurrent heal or user rebind), so no launch frame was sent. Default True
    # for every non-heal caller (they pre-stamp via ``replace_runner_id``).
    repinned: bool = True

async def cancel_managed_launch_tasks() -> None:
    """
    Cancel and await every in-flight background managed launch.

    Lifespan-teardown hook: without it, a slow provision outlives the
    ASGI shutdown and dies wherever the loop teardown happens to kill
    it. Cancellation is deterministic teardown of the TASK only — an
    already-provisioned sandbox is not terminated here (there is no
    time budget for provider calls during shutdown); its armed launch
    token expires with the provider lifetime cap that also reaps the
    sandbox.

    :returns: None once every task has settled (cancellations and any
        in-flight failures are absorbed via ``return_exceptions``).
    """
    tasks = list(_managed_launch_tasks)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

async def _run_managed_launch(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    runner_control_registry: Any | None,
    runner_credential_store: Any | None = None,
    runner_router: RunnerRouter | None = None,
    relaunch_host: Host | None = None,
) -> None:
    """
    Provision a managed sandbox for a session in the background.

    The ``host_type="managed"`` create returns before the sandbox
    exists; this task carries the rest of the pipeline: provision the
    sandbox + start the host (:func:`launch_managed_host`), bind the
    host + workspace to the session row, launch a runner on the host,
    and wait for that runner's tunnel so a message POST rendezvousing
    on *tracker* can forward immediately once the launch settles.

    The same pipeline serves a sandbox RELAUNCH (*relaunch_host* set):
    a message arriving for a session whose managed sandbox died kicks
    this task with the existing host row, and
    :func:`relaunch_managed_host` provisions a new sandbox generation
    under the same host identity instead of minting a new one.

    Every exit path settles the tracker entry — success via
    ``finish`` (the session then looks like any host-bound session),
    failure via ``fail`` with the reason a waiting message POST
    reports. A session deleted mid-provision is detected at the bind
    step and the fresh sandbox is torn down.

    Server shutdown cancels this task (the lifespan teardown calls
    :func:`cancel_managed_launch_tasks`); an already-provisioned
    sandbox then leaks until the provider's lifetime cap reaps it
    (the armed launch token expires with the same cap).

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on auth-disabled servers).
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Parsed repository-URL workspace to clone inside the
        sandbox, or ``None`` for an empty workspace.
    :param tracker: The app's :class:`ManagedLaunchTracker`; this
        session's entry was registered by the caller.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param runner_control_registry: Deprecated runner-tunnel registry slot.
        Retained for callsite compatibility while runner readiness is
        probed through ``runner_router``.
    :param runner_credential_store: Shared fabric credential store used
        to make launch tokens visible to every server replica.
    :param runner_router: Router used to probe the launched runner over
        the NATS control plane before settling readiness.
    :param relaunch_host: Existing managed host row to relaunch a new
        sandbox generation for, or ``None`` for a first launch (a
        fresh host identity is minted).
    """
    managed = await _sessions_facade()._provision_managed_sandbox(
        session_id=session_id,
        owner=owner,
        sandbox_config=sandbox_config,
        repo=repo,
        tracker=tracker,
        host_store=host_store,
        relaunch_host=relaunch_host,
    )
    if managed is None:
        return
    await _sessions_facade()._bind_and_launch_managed_runner(
        session_id=session_id,
        owner=owner,
        managed=managed,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        host_registry=host_registry,
        runner_control_registry=runner_control_registry,
        runner_credential_store=runner_credential_store,
        runner_router=runner_router,
    )

async def _provision_managed_sandbox(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    host_store: HostStore,
    relaunch_host: Host | None,
) -> ManagedHostLaunch | None:
    """
    Run the provision phase of a background managed launch.

    Dispatches to :func:`relaunch_managed_host` (existing host row)
    or :func:`launch_managed_host` (fresh identity) and converts any
    failure into a settled tracker entry — the background task has no
    caller to raise to.

    :param session_id: Session/conversation identifier.
    :param owner: User the managed host acts for.
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Repository workspace to clone, or ``None``.
    :param tracker: The app's launch tracker (failed here on error).
    :param host_store: Persistent host registrations.
    :param relaunch_host: Existing host row for a relaunch, or
        ``None`` for a first launch.
    :returns: The launch result, or ``None`` when the launch failed
        (the tracker entry is already settled with the reason).
    """
    from omnigent.server.managed_hosts import launch_managed_host, relaunch_managed_host

    def _on_stage(stage: str) -> None:
        """
        Relay a launch-pipeline stage to the session's progress surface.

        Passed into the launch helpers, which may invoke it from the
        worker thread their sandbox exec steps run on —
        :func:`_publish_sandbox_status` is thread-safe.

        :param stage: The stage just entered, e.g. ``"cloning"``.
        """
        _sessions_facade()._publish_sandbox_status(session_id, stage)

    try:
        if relaunch_host is not None:
            return await relaunch_managed_host(
                config=sandbox_config,
                host=relaunch_host,
                host_store=host_store,
                repo=repo,
                on_stage=_on_stage,
            )
        return await launch_managed_host(
            config=sandbox_config,
            owner=owner,
            host_store=host_store,
            repo=repo,
            on_stage=_on_stage,
        )
    except HTTPException as exc:
        _logger.warning(
            "Managed sandbox launch failed for session %s: %s",
            session_id,
            exc.detail,
        )
        tracker.fail(session_id, str(exc.detail))
        _sessions_facade()._publish_sandbox_status(session_id, "failed", str(exc.detail))
        return None
    except Exception:
        # Broad on purpose: this is a fire-and-forget task — an
        # unexpected error must settle the tracker (or a waiting
        # message POST hangs until its timeout) and must not escape
        # as an unhandled-task traceback.
        _logger.exception(
            "Managed sandbox launch crashed for session %s",
            session_id,
        )
        tracker.fail(session_id, "internal error during managed sandbox launch")
        _sessions_facade()._publish_sandbox_status(
            session_id, "failed", "internal error during managed sandbox launch"
        )
        return None

async def _await_settled_managed_launch(launch: ManagedLaunch) -> None:
    """
    Block until a managed launch settles, raising its failure.

    The rendezvous a message POST takes when it races a background
    managed launch (create-time provisioning or a dead-sandbox
    relaunch): resolve as soon as the launch settles, surface the
    recorded reason when it failed, and give up with a clear retry
    hint when the launch outlives the rendezvous budget.

    :param launch: The session's tracker entry.
    :raises OmnigentError: 503 when the launch failed or is still
        running at the timeout.
    """
    from omnigent.server.managed_hosts import MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S

    try:
        await asyncio.wait_for(
            launch.settled.wait(),
            timeout=MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise OmnigentError(
            "The session's managed sandbox is still provisioning; try again shortly",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from None
    if launch.error is not None:
        raise OmnigentError(
            f"The session's managed sandbox failed to launch: {launch.error}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

async def _maybe_relaunch_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Relaunch a dead managed sandbox for a session, if it has one.

    Called from the message-dispatch relaunch path when the session's
    host tunnel is gone. For an external (laptop) host that is the end
    of the line, but a managed host's sandbox is RELAUNCHABLE: the
    host row is durable, so a new sandbox generation can be provisioned
    under the same host identity — "send a message to wake the
    sandbox", mirroring how a message relaunches a dead runner on a
    live host.

    Single-flighted through the app's :class:`ManagedLaunchTracker`:
    the first message kicks the background relaunch, concurrent and
    later messages rendezvous on the same entry (the check-then-begin
    below has no ``await`` between check and begin, so it is atomic on
    the event loop). A previously FAILED attempt's retained entry is
    replaced — every new message retries.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (``host_id`` set; caller guards).
    :param app_state: ``request.app.state`` — supplies the host store,
        sandbox config, tracker, and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a relaunch engaged and settled
        successfully (the session row is re-bound; re-resolve the
        runner client). ``False`` when the host is not a managed
        sandbox or managed hosts are not configured — the caller
        falls through to the normal unavailable handling.
    :raises OmnigentError: 503 when the relaunch failed or timed out.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    tracker = getattr(app_state, "managed_launches", None)
    if host_store is None or sandbox_config is None or tracker is None:
        return False
    if conv.host_id is None:
        return False
    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or host.sandbox_provider is None:
        return False
    if await asyncio.to_thread(host_store.is_online, conv.host_id):
        # The host row still reads live (status online with a fresh
        # heartbeat) — the missing tunnel is likely a transient blip
        # on THIS replica and the host will reconnect on its own
        # backoff. Replacing the sandbox now would destroy a healthy
        # workspace; let the message fail unavailable instead. A dead
        # sandbox goes stale within the host liveness TTL, after which
        # the next message lands here and relaunches.
        return False
    launch = tracker.get(session_id)
    if launch is None or launch.settled.is_set():
        _kick_managed_relaunch(
            session_id=session_id,
            conv=conv,
            host=host,
            sandbox_config=sandbox_config,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            app_state=app_state,
        )
        launch = tracker.get(session_id)
    if launch is not None:
        await _await_settled_managed_launch(launch)
    return True

def _kick_managed_relaunch(
    *,
    session_id: str,
    conv: Conversation,
    host: Host,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background relaunch for a dead sandbox.

    Recovers the session's create-time repository workspace from its
    label so the fresh generation re-clones it, registers the tracker
    entry, and schedules :func:`_run_managed_launch` with the existing
    host row.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (supplies the repo label).
    :param host: The dead managed host row to relaunch.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries.
    """
    from omnigent.server.managed_hosts import MANAGED_REPO_LABEL_KEY, parse_repo_workspace

    # Re-clone the repository the session was created with so the
    # fresh generation's workspace matches the create-time state.
    # The label holds the raw create-time value, already validated
    # by the create's parse — a parse failure here means the label
    # was tampered with, and the relaunch proceeds with an empty
    # workspace rather than dying.
    repo = None
    raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
    if raw_repo is not None:
        try:
            repo = parse_repo_workspace(raw_repo)
        except ValueError:
            _logger.warning(
                "Session %s has an unparseable %s label (%r); relaunching with an empty workspace",
                session_id,
                MANAGED_REPO_LABEL_KEY,
                raw_repo,
            )
    _logger.info(
        "Managed sandbox for session %s (host %s) is gone; relaunching a new generation",
        session_id,
        conv.host_id,
    )
    tracker.begin(session_id)
    # Seed the relaunch's progress indicator immediately — the user is
    # typically watching the session page when "wake the sandbox" runs.
    _sessions_facade()._publish_sandbox_status(session_id, "provisioning")
    relaunch_task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=host.owner,
            sandbox_config=sandbox_config,
            repo=repo,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            runner_control_registry=getattr(app_state, "runner_control_registry", None),
            runner_credential_store=getattr(app_state, "runner_credential_store", None),
            runner_router=getattr(app_state, "runner_router", None),
            relaunch_host=host,
        )
    )
    _managed_launch_tasks.add(relaunch_task)
    relaunch_task.add_done_callback(_managed_launch_tasks.discard)

async def _persist_host_launch_failure_turn(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    host_error: str | None,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and a host-launch failure error.

    Used when a message arrives for a host-bound session whose runner is
    dead and the host *refuses* to relaunch because the agent's harness
    isn't configured there (the daemon's structured
    ``harness_not_configured`` reply). The message is the real
    runner-start attempt, so — exactly like a native terminal that can't
    boot (:func:`_persist_native_terminal_failure`) — the server records
    the user's message (so the input is consumed, not silently dropped)
    and a sibling ``type="error"`` item carrying the host's message
    (which names the fix, ``omnigent setup``), then publishes the same
    live error/status events the web renders as an error banner. The host
    binding is left intact so a later message relaunches once the user has
    run setup.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param host_error: The host's human-readable refusal, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``. ``None`` falls back to a generic
        ``omnigent setup`` pointer so the banner is never empty.
    :param runner_router: Router used to resolve a sub-agent's runner for
        the parent-wake forward, or ``None`` in in-process / test setups.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    error = ErrorData(
        source="execution",
        # Stable classifier mirroring the host's wire error code, so the
        # web can special-case the banner if it ever wants to.
        code="harness_not_configured",
        message=(
            host_error
            if host_error
            # Defensive fallback: the daemon always sends a message with
            # the code, but the banner must stay actionable if a
            # third-party host omits it.
            else (
                "the agent's harness is not configured on the selected host — run `omnigent setup`"
            )
        ),
    )
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(conv, user_item, conversation_store)
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(type="error", response_id=turn_id, data=error),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(session_id, "failed", ErrorDetail(code=error.code, message=error.message))
    # A host-launched sub-agent that can't configure must wake its parent,
    # the same way a boot failure does — no-ops for top-level sessions.
    await _forward_native_subagent_terminal_failure(session_id, conv, error, runner_router)
    return consumed.id
