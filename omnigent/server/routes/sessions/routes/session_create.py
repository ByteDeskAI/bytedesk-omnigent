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
from .._constants import *
from .._state import *
from .._access import *  # noqa: F403
from .._create import *  # noqa: F403
from .._elicitation import *  # noqa: F403
from .._external_events import *  # noqa: F403
from .._helpers import *  # noqa: F403
from .._list_updates import *  # noqa: F403
from .._managed_launch import *  # noqa: F403
from .._mcp import *  # noqa: F403
from .._native import *  # noqa: F403
from .._policy import *  # noqa: F403
from .._publish import *  # noqa: F403
from .._resources import *  # noqa: F403
from .._runner import *  # noqa: F403
from .._skills import *  # noqa: F403
from .._snapshot import *  # noqa: F403
from .._subagent import *  # noqa: F403
from .._usage import *  # noqa: F403

def register_session_create(
    router,
    *,
    conversation_store,
    agent_store,
    file_store,
    artifact_store,
    runner_router,
    auth_provider,
    permission_store,
    agent_cache,
    liveness_lookup,
    comment_store,
    runner_tunnel_tokens,
    runner_exit_reports,

):
        # ── POST /sessions ───────────────────────────────────────────

        @router.post(
            "/sessions",
            status_code=201,
            response_model=None,
            # CSRF hardening: this route dispatches on Content-Type (JSON vs
            # multipart bundled-create), so reject text/plain and other simple
            # types up front while still allowing both legitimate body shapes.
            dependencies=[Depends(require_json_or_multipart_content_type)],
        )
        async def create_session(
            request: Request,
        ) -> SessionResponse | CreatedSessionResponse:
            """
            Create a session.

            ``application/json`` preserves the existing contract: bind to
            an already-registered agent by ``agent_id`` and return the full
            session snapshot. ``multipart/form-data`` is the Alpha
            runner-state create path: the request carries a JSON
            ``metadata`` part and a ``bundle`` file part, then the server
            stores the bundle and creates the conversation row plus
            session-scoped agent row in one database transaction.

            :param request: FastAPI request containing either JSON or
                multipart form data.
            :returns: :class:`SessionResponse` for JSON create, or
                :class:`CreatedSessionResponse` for bundled create.
            :raises OmnigentError: If metadata, bundle, or agent lookup
                validation fails, artifact storage is unavailable, or
                database creation fails.
            """
            user_id = _require_user(request, auth_provider)
            # Resolve the full principal (BDP-2388, ADR-0149): the tenant
            # the caller belongs to is persisted on the new session so an
            # external consumer's sessions are tenant-scoped. ``None`` for
            # single-org / local callers (today's default) — zero behavior
            # change. ``get_principal`` is the 2a Adapter over the auth chain.
            # ``auth_provider`` is None in single-user / local mode (the same
            # posture ``_require_user`` already tolerates above), so guard the
            # call — an unguarded ``None.get_principal`` 500s every create in that
            # mode (and broke the host-launch integration harness).
            _principal = auth_provider.get_principal(request) if auth_provider is not None else None
            tenant_id = _principal.tenant_id if _principal is not None else None
            content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type == "multipart/form-data":
                result = await _create_bundled_session_from_multipart(request, user_id, tenant_id)
                if permission_store is not None and user_id is not None:
                    await asyncio.to_thread(permission_store.ensure_user, user_id)
                    await asyncio.to_thread(
                        permission_store.grant, user_id, result.session_id, LEVEL_OWNER
                    )
                # Push the new session to this user's other open tabs so it
                # enters the sidebar without a list poll (WS /sessions/updates).
                _announce_session_added(user_id, result.session_id)
                return result

            try:
                payload = await request.json()
                body = SessionCreateRequest.model_validate(payload)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "type": "json_invalid",
                            "loc": ["body"],
                            "msg": "Invalid JSON",
                            "input": None,
                        },
                    ],
                ) from exc
            except ValidationError as exc:
                # include_context=False: pydantic v2 puts the RAW exception
                # object in ctx for validator-raised ValueErrors, which
                # JSONResponse cannot serialize — every model_validator 422
                # on this route 500'd as internal_error. The human-readable
                # message survives in each entry's `msg`.
                raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc

            # Bind-or-resume / idempotency (BDP-2390, ADR-0149): a stable
            # external_key (request body or Idempotency-Key header) returns the
            # live session for a repeat create instead of a duplicate
            # (EIP Idempotent Receiver + Correlation Identifier). SELECT-first
            # handles the common retry; the partial unique index is the
            # single-writer guard for the concurrent race (caught below).
            external_key = body.external_key or request.headers.get("Idempotency-Key")

            async def _existing_for_key() -> SessionResponse | None:
                if not external_key:
                    return None
                existing = await asyncio.to_thread(
                    conversation_store.get_conversation_by_external_key, external_key
                )
                if existing is None:
                    return None
                level = await _get_permission_level(user_id, existing.id, permission_store)
                return await _get_session_snapshot(
                    conversation_store,
                    existing.id,
                    permission_level=level,
                    agent_store=agent_store,
                    agent_cache=agent_cache,
                    conversation=existing,
                    liveness_lookup=liveness_lookup,
                )

            hit = await _existing_for_key()
            if hit is not None:
                return hit

            try:
                resp = await _create_session_from_existing_agent(
                    conversation_store,
                    agent_store,
                    runner_router,
                    body,
                    request,
                    agent_cache=agent_cache,
                    user_id=user_id,
                    permission_store=permission_store,
                    liveness_lookup=liveness_lookup,
                    tenant_id=tenant_id,
                    external_key=external_key,
                )
            except IntegrityError:
                # A concurrent create won the external_key race; return its
                # session (idempotent) instead of surfacing the constraint error.
                race_hit = await _existing_for_key()
                if race_hit is not None:
                    return race_hit
                raise
            # BDP-2434: stash the user's MCP access token (if Office sent one on this
            # inbound create) keyed by the new session, so the later ``tools/call``
            # mint can fold it into the acting-identity carrier for OBO egress.
            # No-op when the header is absent (degrade-to-default).
            stash_subject_token_from_headers(request.app.state, resp.id, request.headers)
            # Notify the runner about the new session so it can resolve
            # the spec and cache sub_agent_name before the first turn.
            # Without this, the runner doesn't know this session exists
            # until the first forwarded event.
            conv = conversation_store.get_conversation(resp.id)
            # Mark the terminal spin-up flag at creation — the earliest
            # possible point — for a host-launched terminal-first session
            # (claude-native / codex-native). The runner's own pending emit
            # arrives much later (after host launch, runner boot, spec
            # resolve, and harness spawn — each a round-trip), so the spinner
            # would otherwise only flash for the sub-second window before the
            # already-spawned terminal resolves. Gated on host_id because the
            # runner only auto-creates (and thus only clears) a terminal for
            # host-launched sessions; a CLI-bound terminal-first session
            # manages its own terminal and would strand the flag. Clears come
            # from the runner's finally, the relay's resource.created
            # self-heal, or the host-launch-failure path below.
            _terminal_first_create = (
                conv is not None
                and body.host_id is not None
                and conv.labels.get(_CLAUDE_NATIVE_UI_LABEL_KEY) == _CLAUDE_NATIVE_UI_LABEL_VALUE
            )
            if _terminal_first_create:
                _publish_terminal_pending(resp.id, True)
            _rc = await _get_runner_client(resp.id, runner_router)
            if _rc is not None and conv is not None:
                try:
                    await _rc.post(
                        "/v1/sessions",
                        json={
                            "session_id": resp.id,
                            "agent_id": conv.agent_id,
                            "sub_agent_name": conv.sub_agent_name,
                        },
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Failed to notify runner about session %s",
                        resp.id,
                        exc_info=True,
                    )
            # Grant the creator ownership BEFORE any host launch so the
            # launch's session-ownership check (shared with
            # POST /v1/hosts/{host_id}/runners via resolve_host_launch)
            # sees the grant.
            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(permission_store.grant, user_id, resp.id, LEVEL_OWNER)
                resp.permission_level = await _get_permission_level(user_id, resp.id, permission_store)
            # Push the new session to this user's other open tabs (see the
            # multipart path above for the rationale).
            _announce_session_added(user_id, resp.id)

            # Managed host: schedule a BACKGROUND sandbox provision bound
            # to this session and return immediately — provisioning takes
            # tens of seconds and must not block the create POST. The
            # background task binds host + workspace to the session row
            # and launches the runner once the sandbox host registers; a
            # message POST racing the provision rendezvouses on the
            # tracker entry registered here (see post_event). Config
            # problems and malformed repo workspaces still fail the POST
            # synchronously.
            launch_host_id = body.host_id
            if body.host_type == "managed" and resp.runner_id is None:
                sandbox_config = getattr(request.app.state, "sandbox_config", None)
                host_store_for_managed = getattr(request.app.state, "host_store", None)
                managed_launches = getattr(request.app.state, "managed_launches", None)
                if (
                    sandbox_config is None
                    or host_store_for_managed is None
                    or managed_launches is None
                ):
                    raise OmnigentError(
                        "managed hosts are not configured on this server — add a "
                        "'sandbox:' section to the server config",
                        code=ErrorCode.INVALID_INPUT,
                    )
                from omnigent.server.auth import RESERVED_USER_LOCAL
                from omnigent.server.managed_hosts import (
                    MANAGED_REPO_LABEL_KEY,
                    parse_repo_workspace,
                )

                # A managed workspace is a repository URL (schema-
                # validated) the launch clones inside the sandbox; parse
                # it now so a malformed URL is a synchronous 4xx, not a
                # background failure.
                repo = parse_repo_workspace(body.workspace) if body.workspace is not None else None
                if body.workspace is not None:
                    # The session row's workspace is overwritten with the
                    # CLONED path at bind time; record the raw request
                    # value so a sandbox relaunch can re-clone the same
                    # repository into the new generation.
                    await asyncio.to_thread(
                        conversation_store.set_labels,
                        resp.id,
                        {MANAGED_REPO_LABEL_KEY: body.workspace},
                    )
                managed_launches.begin(resp.id)
                # Seed the launch-progress indicator before the background
                # task starts, so the first GET snapshot (the Web UI
                # navigates to the session page immediately after this
                # 201) already carries the "provisioning" stage.
                _publish_sandbox_status(resp.id, "provisioning")
                launch_task = asyncio.create_task(
                    _run_managed_launch(
                        session_id=resp.id,
                        # On auth-disabled servers user_id is None; the
                        # sandbox host registers under the reserved local
                        # owner, same as a directly-connected host would.
                        owner=user_id if user_id is not None else RESERVED_USER_LOCAL,
                        sandbox_config=sandbox_config,
                        repo=repo,
                        tracker=managed_launches,
                        conversation_store=conversation_store,
                        host_store=host_store_for_managed,
                        host_registry=getattr(request.app.state, "host_registry", None),
                        runner_control_registry=getattr(
                            request.app.state,
                            "runner_control_registry",
                            None,
                        ),
                        runner_credential_store=getattr(
                            request.app.state,
                            "runner_credential_store",
                            None,
                        ),
                        runner_router=getattr(request.app.state, "runner_router", None),
                    )
                )
                _managed_launch_tasks.add(launch_task)
                launch_task.add_done_callback(_managed_launch_tasks.discard)

            # Host launch: if a host is targeted (caller-supplied or
            # managed) and no runner is bound yet, authorize (caller must
            # own the host AND the session), atomically bind, then launch.
            # Same authorization path as POST /v1/hosts/{host_id}/runners.
            if launch_host_id is not None and resp.runner_id is None:
                host_registry = getattr(request.app.state, "host_registry", None)
                host_store_inst = getattr(request.app.state, "host_store", None)
                if host_registry is not None and host_store_inst is not None:
                    from omnigent.server.routes._host_launch import resolve_host_launch_access

                    await asyncio.to_thread(
                        resolve_host_launch_access,
                        user_id=user_id,
                        host_id=launch_host_id,
                        session_id=resp.id,
                        host_store=host_store_inst,
                        conversation_store=conversation_store,
                        permission_store=permission_store,
                    )
                    if not await asyncio.to_thread(host_store_inst.is_online, launch_host_id):
                        raise HTTPException(status_code=409, detail="host is offline")
                    if resp.workspace is None:  # pragma: no cover — schema guards
                        raise OmnigentError(
                            "session has host_id but no workspace; "
                            "schema constraint should have prevented this",
                            code=ErrorCode.INTERNAL_ERROR,
                        )
                    _create_runner_control_registry = getattr(
                        request.app.state, "runner_control_registry", None
                    )
                    try:
                        launch_attempt = await HostWorkerRunnerFabric().ensure_runner(
                            HostRunnerAcquisition(
                                session_id=resp.id,
                                host_id=launch_host_id,
                                workspace=resp.workspace,
                                # Already canonical (see _resolve_harness); lets
                                # the host refuse an unconfigured harness before
                                # spawning. None (agent not resolvable) skips the
                                # host-side check.
                                harness=resp.harness,
                                conversation_store=conversation_store,
                                host_registry=host_registry,
                                owner=user_id,
                                runner_control_registry=_create_runner_control_registry,
                                runner_credential_store=getattr(
                                    request.app.state,
                                    "runner_credential_store",
                                    None,
                                ),
                                bind_mode="set",
                                timeout_s=30.0,
                            )
                        )
                    except FabricRunnerConflict as exc:
                        raise OmnigentError(
                            f"Session {resp.id!r} already has a runner bound",
                            code=ErrorCode.CONFLICT,
                        ) from exc
                    if not launch_attempt.acked:
                        result = {
                            "status": "failed",
                            "error": launch_attempt.error or "host launch timed out",
                        }
                    elif launch_attempt.error_code is not None or launch_attempt.error is not None:
                        result = {
                            "status": "failed",
                            "error": launch_attempt.error,
                            "error_code": launch_attempt.error_code,
                        }
                    else:
                        result = {"status": "ok"}
                    if result.get("status") == "failed":
                        # Lenient on every create-time launch failure, including
                        # an unconfigured harness: the picker's readiness data
                        # can be stale (the user may have run `omnigent setup`
                        # since the host last connected), so we never block the
                        # create. The session opens with the binding intact; the
                        # first message drives the real runner start, and if the
                        # host still refuses there, that path consults the daemon
                        # and persists a transcript error (see post_event's
                        # relaunch branch). No create-time harness gating.
                        _logger.warning(
                            "Host %s failed to launch runner for session %s: %s",
                            launch_host_id,
                            resp.id,
                            result.get("error"),
                        )
                        # The runner never booted, so its pending=False clear
                        # will never fire. Clear the spin-up flag here so a
                        # failed launch doesn't strand the Terminal-pill
                        # spinner. No-op when we never set it.
                        if _terminal_first_create:
                            _publish_terminal_pending(resp.id, False)
                    resp.runner_id = launch_attempt.runner_id
                    resp.host_id = launch_host_id

            return resp

        async def _create_bundled_session_from_multipart(
            request: Request,
            user_id: str | None,
            tenant_id: str | None = None,
        ) -> CreatedSessionResponse:
            """
            Handle multipart ``POST /v1/sessions`` with inline agent upload.

            :param request: FastAPI request containing ``metadata`` and
                ``bundle`` form parts.
            :param user_id: Authenticated caller, e.g.
                ``"alice@example.com"``. Used to authorize
                ``metadata.parent_session_id`` and enforce
                runner ownership on parent inheritance.
            :returns: :class:`CreatedSessionResponse` with the new
                session id.
            :raises HTTPException: 422 when a required multipart part is
                absent.
            :raises OmnigentError: If metadata or bundle validation
                fails, or ``parent_session_id`` fails authorization.
            """
            if artifact_store is None:
                raise OmnigentError(
                    "artifact store is not configured",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            form = await request.form()
            metadata = form.get("metadata")
            bundle = form.get("bundle")
            missing = [
                _multipart_missing_detail(field)
                for field, value in (("metadata", metadata), ("bundle", bundle))
                if value is None
            ]
            if missing:
                raise HTTPException(status_code=422, detail=missing)
            if not isinstance(metadata, str):
                raise HTTPException(status_code=422, detail=[_multipart_missing_detail("metadata")])
            if not isinstance(bundle, StarletteUploadFile):
                raise HTTPException(status_code=422, detail=[_multipart_missing_detail("bundle")])
            parsed_metadata = _parse_session_create_metadata(metadata)
            _reject_reserved_cost_control_label_seed(parsed_metadata.labels)

            inherited_runner_id: str | None = None
            if parsed_metadata.parent_session_id is not None:
                inherited_runner_id = await _authorize_bundled_parent_and_inherit_runner(
                    parsed_metadata.parent_session_id,
                    user_id=user_id,
                    permission_store=permission_store,
                    conversation_store=conversation_store,
                    runner_router=runner_router,
                )

            bundle_bytes = await bundle.read()
            result = await asyncio.to_thread(
                _create_session_from_bundle,
                agent_store,
                conversation_store,
                artifact_store,
                parsed_metadata,
                bundle_bytes,
                inherited_runner_id,
                tenant_id,
            )
            # Top-level creates (no inherited runner) skip the notify —
            # their runner registers itself later.
            if inherited_runner_id is not None:
                await _notify_runner_of_bundled_child(
                    result.session_id,
                    result.agent_id,
                    runner_router,
                )
            return result

