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

async def _launch_runner_on_host(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    *,
    owner: str | None = None,
    runner_control_registry: Any | None = None,
    runner_credential_store: Any | None = None,
    repin: Callable[[str], bool] | None = None,
) -> _HostLaunchAttempt:
    """
    Ask a host to spawn a runner for a session and capture the result.

    Generates a new binding token, writes the runner_id to the session
    row, sends ``host.launch_runner`` (carrying the session's canonical
    harness so the host can refuse an unconfigured one), and waits up to
    :data:`_HOST_LAUNCH_RESULT_TIMEOUT_S` for the host's result frame.
    Does NOT wait for the runner to *connect* — the caller polls for that
    separately; this only captures the spawn/refuse verdict so a
    structured refusal (harness not configured) can be surfaced instead
    of silently timing out as ``RUNNER_UNAVAILABLE``.

    :param conv: The conversation that needs a runner.
    :param conversation_store: Store for updating ``runner_id``.
    :param host_registry: In-memory ``HostRegistry``.
    :param host_conn: The live ``HostConnection`` for the host.
    :param owner: Authenticated owner this runner is launched for, e.g.
        ``"alice@example.com"``. Recorded as the runner's TRUSTED launch
        owner (BDP-2436) so the tunnel handler can resolve ownership for
        the token-only runner under accounts mode — the runner presents
        its binding token but no user cookie, so its owner cannot be read
        from the handshake. ``None`` (no-auth / single-user mode) records
        nothing; the loopback runner resolves to the reserved local
        identity at connect as before.
    :param runner_control_registry: Runner-tunnel registry to record the launch
        owner in. ``None`` (minimal test wirings) skips recording.
    :param runner_credential_store: Shared fabric credential store used
        to make launch tokens visible to every server replica.
    :param repin: Optional self-heal compare-and-swap callback. When supplied,
        it owns the runner_id swap and aborts launch on a lost CAS.
    :returns: The :class:`_HostLaunchAttempt` — the new runner id plus any
        structured refusal from the host.
    """
    # Pull workspace from the session row — populated and validated
    # at session create per designs/SESSION_WORKSPACE_SELECTION.md.
    # The check constraint guarantees workspace is non-NULL when
    # host_id is set, so this assertion is a tripwire for any path
    # that bypassed the validation.
    if conv.workspace is None:  # pragma: no cover — constraint guards
        _logger.error(
            "session %s has host_id=%s but workspace is NULL — schema "
            "constraint should have prevented this",
            conv.id,
            conv.host_id,
        )
        return _HostLaunchAttempt(runner_id=conv.runner_id or "")
    attempt = await HostWorkerRunnerFabric().ensure_runner(
        HostRunnerAcquisition(
            session_id=conv.id,
            host_id=host_conn.host_id,
            workspace=conv.workspace,
            # Canonical harness (see _resolve_harness) so the host runs the
            # same configuration check it does at create-time launch. None
            # (agent not resolvable) skips the host-side check — fail open.
            harness=_resolve_harness(conv),
            conversation_store=conversation_store,
            host_registry=host_registry,
            owner=owner,
            runner_control_registry=runner_control_registry,
            runner_credential_store=runner_credential_store,
            bind_mode="replace",
            timeout_s=_HOST_LAUNCH_RESULT_TIMEOUT_S,
            host_connection=host_conn,
            # Self-heal CAS swap (BDP-2579 F3) — the fabric runs ``repin``
            # in place of its bind and aborts (no launch frame) if it loses.
            repin=repin,
        )
    )
    if not attempt.repinned:
        return _HostLaunchAttempt(runner_id=attempt.runner_id, repinned=False)
    if not attempt.acked:
        return _HostLaunchAttempt(runner_id=attempt.runner_id, acked=False)
    if attempt.error_code is not None or attempt.error is not None:
        return _HostLaunchAttempt(
            runner_id=attempt.runner_id,
            error_code=attempt.error_code,
            error=attempt.error,
        )
    return _HostLaunchAttempt(runner_id=attempt.runner_id)

async def _launch_runner_on_host_id(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_id: str,
    *,
    owner: str | None = None,
    runner_control_registry: Any | None = None,
    runner_credential_store: Any | None = None,
    repin: Callable[[str], bool] | None = None,
) -> _HostLaunchAttempt:
    """
    Ask a host to spawn a runner when its tunnel may live on another replica.

    Mirrors :func:`_launch_runner_on_host` but routes through
    ``host_control`` so a REST request served by replica A can launch through a
    host tunnel owned by replica B. ``repin`` follows the same self-heal CAS
    contract as :func:`_launch_runner_on_host` (BDP-2579 F3).
    """
    if conv.workspace is None:  # pragma: no cover — constraint guards
        _logger.error(
            "session %s has host_id=%s but workspace is NULL — schema "
            "constraint should have prevented this",
            conv.id,
            conv.host_id,
        )
        return _HostLaunchAttempt(runner_id=conv.runner_id or "")
    attempt = await HostWorkerRunnerFabric().ensure_runner(
        HostRunnerAcquisition(
            session_id=conv.id,
            host_id=host_id,
            workspace=conv.workspace,
            harness=_resolve_harness(conv),
            conversation_store=conversation_store,
            host_registry=host_registry,
            owner=owner,
            runner_control_registry=runner_control_registry,
            runner_credential_store=runner_credential_store,
            bind_mode="replace",
            timeout_s=_HOST_LAUNCH_RESULT_TIMEOUT_S,
            # Self-heal CAS swap (BDP-2579 F3) — see _launch_runner_on_host.
            repin=repin,
        )
    )
    if not attempt.repinned:
        return _HostLaunchAttempt(runner_id=attempt.runner_id, repinned=False)
    if attempt.error is not None and not attempt.acked:
        _logger.warning(
            "Host %s could not launch runner for %s: %s",
            host_id,
            conv.id,
            attempt.error,
        )
        return _HostLaunchAttempt(runner_id=attempt.runner_id, acked=False)
    if not attempt.acked:
        return _HostLaunchAttempt(runner_id=attempt.runner_id, acked=False)
    if attempt.error_code is not None or attempt.error is not None:
        return _HostLaunchAttempt(
            runner_id=attempt.runner_id,
            error_code=attempt.error_code,
            error=attempt.error,
        )
    return _HostLaunchAttempt(runner_id=attempt.runner_id)

async def _bind_and_launch_managed_runner(
    *,
    session_id: str,
    owner: str,
    managed: ManagedHostLaunch,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    runner_control_registry: Any | None,
    runner_credential_store: Any | None = None,
    runner_router: RunnerRouter | None = None,
) -> None:
    """
    Bind a provisioned managed host to its session and launch a runner.

    The bind step doubles as the delete-race detector: a session
    deleted while its sandbox provisioned surfaces here as
    ``ConversationNotFoundError``, and the fresh sandbox is torn down
    (the delete route could not see the host binding yet). Settles
    the tracker on every path.

    :param session_id: Session/conversation identifier.
    :param owner: User the managed host acts for — recorded as the
        runner's trusted launch owner so its token-only tunnel resolves
        ownership under accounts mode (BDP-2436).
    :param managed: The provision result (host id + workspace).
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param runner_control_registry: Deprecated runner-tunnel registry slot.
        Retained for compatibility while readiness is probed over NATS.
    :param runner_credential_store: Shared fabric credential store used
        to make launch tokens visible to every server replica.
    :param runner_router: Router used to probe the launched runner's
        control-plane readiness. ``None`` in minimal test wirings.
    """
    from omnigent.server.managed_hosts import terminate_managed_host

    try:
        conv = await asyncio.to_thread(
            conversation_store.set_host_id,
            session_id,
            managed.host_id,
            managed.workspace,
        )
    except ConversationNotFoundError:
        # The session was deleted while its sandbox provisioned. The
        # delete route couldn't see the host binding yet, so tear the
        # fresh sandbox down here (deleting the host row also revokes
        # its launch token).
        _logger.info(
            "Session %s was deleted during managed provisioning; "
            "terminating fresh sandbox on host %s",
            session_id,
            managed.host_id,
        )
        host = await asyncio.to_thread(host_store.get_host, managed.host_id)
        if host is not None:
            await terminate_managed_host(host, host_store, sandbox_config)
        tracker.fail(session_id, "session was deleted while its sandbox was provisioning")
        _publish_sandbox_status(
            session_id, "failed", "session was deleted while its sandbox was provisioning"
        )
        return
    # Host bound; what remains is launching the runner and waiting
    # for its tunnel.
    _publish_sandbox_status(session_id, "connecting")
    runner_id: str | None = None
    if host_registry is not None:
        host_conn = host_registry.get(managed.host_id)
        if host_conn is not None:
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
                owner=owner,
                runner_control_registry=runner_control_registry,
                runner_credential_store=runner_credential_store,
            )
        else:
            launch_attempt = await _launch_runner_on_host_id(
                conv,
                conversation_store,
                host_registry,
                managed.host_id,
                owner=owner,
                runner_control_registry=runner_control_registry,
                runner_credential_store=runner_credential_store,
            )
        if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
            # The sandbox image should bake in the harness, but if the
            # host refuses, fail the launch loudly (mirroring the
            # delete-during-provisioning path) rather than waiting out
            # the connect timeout for a runner that will never appear.
            reason = launch_attempt.error or "harness not configured on the sandbox host"
            tracker.fail(session_id, reason)
            _publish_sandbox_status(session_id, "failed", reason)
            return
        runner_id = launch_attempt.runner_id
    if runner_id is not None and runner_router is not None:
        # Wait for the runner control plane before settling so a rendezvoused
        # message POST resolves its runner client on the first try. A timeout
        # still settles successfully — the host is bound, and post_event's
        # normal host-relaunch path owns dead runners.
        await _wait_for_runner_client(
            session_id,
            runner_router,
            runner_control_registry,
            runner_id=runner_id,
            timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
        )
    tracker.finish(session_id)
    _publish_sandbox_status(session_id, "ready")

async def _ensure_runner_session_initialized(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
) -> None:
    """
    Drive — and wait for — the runner's session-init handshake.

    Posts ``POST /v1/sessions`` to a freshly (re)launched runner and
    awaits it, so the runner's ``create_session`` completes before the
    caller forwards a message. For a claude-native session that means
    the tmux terminal **and its transcript forwarder are watching**
    before the web message is injected into the TUI — the round-trip
    that promotes the optimistic bubble and streams the reply only
    happens if the forwarder is in place first.

    This closes the host-restart race: today the auto-relaunch /
    resume paths wait only for the runner's *tunnel* to register
    (``runner_client`` becomes non-None), not for the session
    handshake, so the message can be injected before the forwarder
    attaches and is lost. The new / runner-bound paths don't hit this
    because they run the handshake as a distinct step before any
    message (``create_session`` endpoint) or against a from-offset-0
    forwarder.

    The runner's ``create_session`` is idempotent (it skips terminal
    auto-create under a per-session lock when one already exists), so
    this is safe even though ``_on_runner_connect`` (server/app.py)
    also posts ``/v1/sessions`` on the same connection — whichever
    lands first creates the terminal; the other no-ops.

    Best-effort and matching the create / PATCH handshakes: a transport
    error is logged and swallowed (the relay + ``_on_runner_connect``
    are the backstop), but the *await* — the actual fix — still
    serializes the handshake ahead of the caller's message forward.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*; supplies
        ``agent_id`` and ``sub_agent_name`` for the handshake body.
    :param runner_client: Runner client already resolved for
        *session_id* (its tunnel is up).
    :returns: None.
    """
    try:
        resp = await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": conv.agent_id,
                "sub_agent_name": conv.sub_agent_name,
            },
            timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
        )
        # httpx only raises on transport errors; a 4xx/5xx means create_session
        # likely didn't run (terminal + forwarder not set up), so surface it
        # via the same warning path rather than silently forwarding into a
        # half-initialized runner.
        resp.raise_for_status()
        _publish_runner_recovered_status(session_id)
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Session-init handshake to runner failed for session %s; "
            "forwarding the message anyway",
            session_id,
            exc_info=True,
        )

