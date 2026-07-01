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

def _multipart_missing_detail(field: str) -> dict[str, Any]:
    """
    Build a FastAPI-style missing multipart field error.

    :param field: Missing form field name, e.g. ``"bundle"``.
    :returns: A validation-detail dict for HTTP 422 responses.
    """
    return {
        "type": "missing",
        "loc": ["body", field],
        "msg": "Field required",
        "input": None,
    }

def _require_host_conn_for_worktree(host_id: str | None, request: Request) -> HostConnection:
    """
    Resolve the live host connection for a worktree operation.

    :param host_id: Target host id from the session request, e.g.
        ``"host_a1b2c3d4..."``. ``None`` is rejected — git worktree
        creation requires a host (the server has no filesystem).
    :param request: FastAPI request carrying ``app.state.host_registry``.
    :returns: The live :class:`HostConnection` for ``host_id``.
    :raises OmnigentError: ``invalid_input`` when ``host_id`` is
        ``None``; ``internal_error`` when no host registry is
        configured; ``conflict`` when the host is offline.
    """
    if host_id is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        # Server misconfiguration, not bad client input — mirror
        # _validate_session_workspace, which also returns internal_error.
        raise OmnigentError(
            "host registry is not configured; cannot create a worktree",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    return host_conn

async def _create_session_worktree(
    *,
    host_id: str | None,
    source_repo: str | None,
    git: SessionGitOptions,
    request: Request,
) -> CreatedWorktree:
    """
    Create a git worktree on the host for a new session branch.

    Validates the branch name server-side (the host re-validates), then
    proxies ``host.create_worktree``. The returned worktree path
    becomes the session ``workspace``. See
    designs/SESSION_GIT_WORKTREE.md.

    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
        Required (worktree creation needs a host).
    :param source_repo: Canonical path of the picked source repo (the
        boundary-validated workspace), e.g. ``"/Users/alice/myrepo"``.
        ``None`` is a programming error and fails loud.
    :param git: Validated git options (``branch_name``, optional
        ``base_branch``).
    :param request: FastAPI request carrying the host registry.
    :returns: The created worktree's ``worktree_path`` (to store as
        ``workspace``) and ``branch`` (to store as ``git_branch``).
    :raises OmnigentError: ``invalid_input`` for a bad branch name,
        missing source repo, or a host-reported git failure (duplicate
        branch, bad base ref, not a repo); ``conflict`` when the host is
        offline or unresponsive; ``internal_error`` when no host registry
        is configured.
    """
    from omnigent.host.git_worktree import WorktreeError, validate_branch_name
    from omnigent.server.routes._host_worktree import (
        WorktreeHostUnavailableError,
        WorktreeProxyError,
        create_worktree_on_host,
    )

    if source_repo is None:  # pragma: no cover — host_id guarantees a workspace
        raise OmnigentError(
            "git worktree creation requires a source repository workspace",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        validate_branch_name(git.branch_name)
    except WorktreeError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

    host_conn = _require_host_conn_for_worktree(host_id, request)
    host_registry = request.app.state.host_registry
    try:
        return await create_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            repo_path=source_repo,
            branch_name=git.branch_name,
            base_branch=git.base_branch,
        )
    except WorktreeHostUnavailableError as exc:
        # Host offline / unresponsive — infra, not user input.
        raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
    except WorktreeProxyError as exc:
        # Host-reported git failure (dup branch, bad base, not a repo) —
        # user-correctable input.
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

async def _remove_session_worktree_best_effort(
    *,
    host_id: str,
    worktree_path: str,
    branch: str,
    delete_branch: bool,
    request: Request,
    reason: str,
) -> None:
    """
    Best-effort removal of a session's git worktree.

    Used for create-rollback (orphan cleanup) and opt-in session-delete
    cleanup. Never raises — a failure is logged so the caller's primary
    operation still completes.

    :param host_id: Host that owns the worktree, e.g.
        ``"host_a1b2c3d4..."``.
    :param worktree_path: Absolute worktree directory to remove on the
        host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch checked out in the worktree, e.g.
        ``"feature/login"``.
    :param delete_branch: When ``True``, also run ``git branch -D``
        after removing the worktree directory.
    :param request: FastAPI request carrying the host registry.
    :param reason: Short label for log lines, e.g.
        ``"create-rollback"`` or ``"session-delete"``.
    """
    from omnigent.server.routes._host_worktree import (
        WorktreeProxyError,
        remove_worktree_on_host,
    )

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        return
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        _logger.warning(
            "Skipping worktree removal (%s) for %s: host %s offline",
            reason,
            worktree_path,
            host_id,
        )
        return
    try:
        await remove_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
        )
    except WorktreeProxyError:
        _logger.warning(
            "Best-effort worktree removal (%s) failed for %s",
            reason,
            worktree_path,
            exc_info=True,
        )

def _resolve_subagent_spec(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> AgentSpec | None:
    """
    Load the parent bundle and resolve a child sub-agent's trusted spec.

    This is the single trusted source for any per-sub-agent launch wiring
    the server derives at create time (terminal-first labels, YOLO
    pass-through args). The spec comes from the server-loaded parent
    bundle — never from caller-supplied request fields — so a caller
    cannot smuggle in launch config a sub-agent's own bundle did not
    declare.

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns ``None``).
    :returns: The matching child :class:`AgentSpec`, or ``None`` when the
        cache is absent, the bundle fails to load, or no sub-agent matches.
    """
    if agent_cache is None:
        return None
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        parent_spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        ).spec
    except Exception:  # noqa: BLE001 -- create-time resolution is best-effort; never block create.
        # A bundle that fails to load here must not break session
        # creation; the session still works, just without the
        # derived labels / launch args.
        _logger.warning(
            "Could not load bundle for agent %s to resolve sub-agent %r spec",
            agent.id,
            sub_agent_name,
            exc_info=True,
        )
        return None
    return _find_spec_by_name(parent_spec, sub_agent_name)

async def _create_session_from_existing_agent(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    body: SessionCreateRequest,
    request: Request,
    agent_cache: AgentCache | None = None,
    user_id: str | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    tenant_id: str | None = None,
    external_key: str | None = None,
) -> SessionResponse:
    """
    Create a session bound to an already-registered agent.

    This preserves the existing JSON ``POST /v1/sessions`` contract:
    clients that uploaded an agent separately still bind by durable
    ``agent_id`` and receive the full session snapshot. Operator-facing
    callers may also pass a template agent's stable ``name``; it is
    normalized to the durable id before persistence.

    :param conversation_store: Store for conversation persistence.
    :param agent_store: Store for agent lookup by durable id.
    :param runner_router: Runner router used to validate any initial
        dispatch triggered by ``initial_items``.
    :param body: Validated JSON create request.
    :param agent_cache: Optional cache for loading parsed agent specs
        from bundles, used to populate ``llm_model`` and
        ``context_window`` in the response.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. Used to authorize parent-session
        and agent ownership and enforce runner
        ownership on parent-session inheritance.
    :param permission_store: Permission store for session-access
        checks. Required for authorization of
        ``parent_session_id`` and session-scoped ``agent_id``.
    :param liveness_lookup: Optional session-scoped liveness lookup
        to populate ``SessionResponse.runner_online``.
    :returns: The newly created session snapshot.
    :raises OmnigentError: 404 if no agent matches ``body.agent_id``;
        403/404 if ``parent_session_id`` or session-scoped ``agent_id``
        fails authorization.
    """
    _reject_reserved_cost_control_label_seed(body.labels)

    agent = await asyncio.to_thread(require_agent_ref, agent_store, body.agent_id)

    # Session-scoped agents belong to a specific session.
    # The caller must have at least READ access to that owning
    # session — otherwise they can execute another user's private
    # agent by guessing the raw agent id.
    if agent.session_id is not None:
        await _require_access(
            user_id,
            agent.session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # Authorize parent_session_id before inheriting anything.
    # The caller must own or have READ access to the parent session;
    # otherwise a forged parent link lets them inherit runner
    # bindings and establish a parent-child relationship with a
    # session they don't control.
    if body.parent_session_id is not None:
        await _require_access(
            user_id,
            body.parent_session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # The persisted override reaches a native CLI as a ``--model`` argv
    # element at terminal launch, so reject shell-/flag-shaped values
    # before any row or worktree exists.
    model_override: str | None = None
    if body.model_override is not None:
        try:
            model_override = validate_model_override(body.model_override)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid model_override: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    # Validated before any row exists so a bad value never creates an
    # orphan session; None (unset) defers to the spec default.
    cost_control_mode_override = _validated_cost_control_mode_override(
        body.cost_control_mode_override
    )

    # Validated against the loaded spec (known harness + omnigent
    # executor type) before any row exists, mirroring the CLI's
    # --harness fail-loud rules.
    harness_override = await asyncio.to_thread(
        _validated_harness_override, body.harness_override, agent
    )

    # Inherit runner affinity from the parent session so the child
    # is assigned to the same runner (sub-agent co-location).
    inherited_runner_id: str | None = None
    if body.parent_session_id is not None:
        parent_conv = conversation_store.get_conversation(body.parent_session_id)
        if parent_conv is not None:
            inherited_runner_id = parent_conv.runner_id
            # Defense-in-depth: don't inherit a runner the
            # caller doesn't own.
            if (
                inherited_runner_id is not None
                and user_id is not None
                and runner_router is not None
            ):
                runner_owner = runner_router.runner_owner(inherited_runner_id)
                if runner_owner is not None and runner_owner != user_id:
                    inherited_runner_id = None

    # Workspace validation: if the caller is binding to a host,
    # they must also pass a workspace, and the workspace must
    # satisfy the agent's os_env.cwd boundary on that host (per
    # designs/SESSION_WORKSPACE_SELECTION.md). Done before
    # create_conversation so a bad workspace never produces a row.
    # With git worktree creation, the validated path is the source
    # repo; the worktree it produces becomes the stored workspace.
    canonical_workspace: str | None = body.workspace
    if body.host_id is not None:
        canonical_workspace = await _validate_session_workspace(
            user_id=user_id,
            host_id=body.host_id,
            workspace=body.workspace,
            agent=agent,
            agent_cache=agent_cache,
            request=request,
        )

    # Git worktree creation (optional): the worktree becomes the
    # stored workspace and its branch is recorded.
    git_branch: str | None = None
    if body.git is not None:
        created_worktree = await _create_session_worktree(
            host_id=body.host_id,
            source_repo=canonical_workspace,
            git=body.git,
            request=request,
        )
        canonical_workspace = created_worktree.worktree_path
        git_branch = created_worktree.branch

    # Native-terminal pass-through args.
    #
    # Named sub-agent creates (``body.sub_agent_name`` set) DERIVE these
    # from the trusted, server-loaded sub-spec only — any caller-supplied
    # ``body.terminal_launch_args`` is ignored. This is the YOLO seam: a
    # native worker bundle declaring ``permission_mode`` / ``yolo: true``
    # gets the corresponding full-bypass flag so it can edit in a
    # headless pane, and a caller cannot inject launch wiring by
    # smuggling args through the spawn body.
    #
    # Sessions that resolve their own agent (top-level sessions and the
    # manual Add Agent child flow where ``sub_agent_name`` is null) keep
    # the validated body args (e.g. ``["--permission-mode",
    # "bypassPermissions"]`` from the web permission-mode selector). The
    # flat-list shape plus this bounds check is the security boundary;
    # mirrors the multipart create + PATCH paths.
    sub_spec: AgentSpec | None = None
    if body.sub_agent_name:
        sub_spec = _resolve_subagent_spec(
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )
        try:
            validated_launch_args = (
                _derive_terminal_launch_args_from_spec(sub_spec) if sub_spec is not None else None
            )
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args in sub-agent spec: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    adopted_existing_child = False
    try:
        conv = conversation_store.create_conversation(
            agent_id=agent.id,
            title=body.title,
            parent_conversation_id=body.parent_session_id,
            runner_id=inherited_runner_id,
            kind="sub_agent" if body.parent_session_id else "default",
            sub_agent_name=body.sub_agent_name,
            host_id=body.host_id,
            workspace=canonical_workspace,
            git_branch=git_branch,
            terminal_launch_args=validated_launch_args,
            tenant_id=tenant_id,
            external_key=external_key,
        )
    except NameAlreadyExistsError:
        # A retry can arrive after the first create minted the child row
        # but timed out before delivering initial_items. Adopt the matching
        # child so the normal post-create path below can finish the work.
        if git_branch is not None and canonical_workspace is not None and body.host_id is not None:
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=canonical_workspace,
                branch=git_branch,
                delete_branch=True,
                request=request,
                reason="create-rollback",
            )
        if body.parent_session_id is None or body.title is None:
            raise
        existing = await asyncio.to_thread(
            _find_subagent_child_by_title,
            conversation_store,
            body.parent_session_id,
            body.title,
        )
        if (
            existing is None
            or existing.agent_id != agent.id
            or existing.sub_agent_name != body.sub_agent_name
        ):
            raise
        if existing.runner_id is None and inherited_runner_id is not None:
            await asyncio.to_thread(
                conversation_store.set_runner_id,
                existing.id,
                inherited_runner_id,
            )
            existing = await asyncio.to_thread(conversation_store.get_conversation, existing.id)
            if existing is None:
                raise
        conv = existing
        adopted_existing_child = True
    except Exception:
        # Broad catch is intentional: ANY create_conversation failure
        # (integrity error, name clash, ...) must trigger orphan-worktree
        # cleanup before the error propagates. We re-raise unchanged
        # below, so nothing is swallowed. git_branch is set only on
        # worktree success.
        if git_branch is not None and canonical_workspace is not None and body.host_id is not None:
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=canonical_workspace,
                branch=git_branch,
                delete_branch=True,
                request=request,
                reason="create-rollback",
            )
        raise
    if (
        model_override is not None
        or cost_control_mode_override is not None
        or harness_override is not None
    ):
        # ``create_conversation`` has no override params; reuse the
        # PATCH path's store write before the runner reads the snapshot
        # (the first turn / terminal launch happens only after this
        # create returns and the caller posts a message event).
        updated_conv = await asyncio.to_thread(
            conversation_store.update_conversation,
            conv.id,
            model_override=model_override,
            cost_control_mode_override=cost_control_mode_override,
            harness_override=harness_override,
        )
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while persisting session overrides",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    # Set wrapper labels at creation time if the agent is a native
    # terminal wrapper, so all messages
    # (including early ones sent before the runner connects) take
    # the native path and avoid double-persistence with the
    # transcript forwarder.
    native_agent = native_coding_agent_for_agent_name(agent.name)
    if native_agent is not None:
        _native_labels = dict(body.labels) if body.labels else {}
        _native_labels.update(native_agent.presentation_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _native_labels)
        conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
    elif (
        body.sub_agent_name
        and sub_spec is not None
        and (_sa_labels := _native_subagent_wrapper_labels_from_spec(sub_spec))
    ):
        # A native-harness sub-agent (claude-native / codex-native) must
        # render terminal-first with the Chat/Terminal pill, same as a
        # top-level wrapper session. Merge over any caller-supplied labels.
        _merged = dict(body.labels) if body.labels else {}
        _merged.update(_sa_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _merged)
        conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
    elif body.labels:
        await asyncio.to_thread(conversation_store.set_labels, conv.id, body.labels)
    initial_items = body.initial_items
    if initial_items and adopted_existing_child:
        existing_cursor: str | None = None
        has_existing_content = False
        while True:
            existing_items = await asyncio.to_thread(
                conversation_store.list_items,
                conv.id,
                limit=100,
                after=existing_cursor,
                order="asc",
            )
            if any(item.type not in NON_CONTENT_ITEM_TYPES for item in existing_items.data):
                has_existing_content = True
                break
            if not existing_items.has_more or existing_items.last_id is None:
                break
            existing_cursor = existing_items.last_id
        if has_existing_content:
            initial_items = []
    if initial_items:
        runner_client = await _get_runner_client(conv.id, runner_router)
        if runner_client is None:
            # No runner bound — persist initial items as history-only
            # seed via the conversation store. No execution fires; the
            # caller is responsible for binding a runner and posting a
            # follow-up event if they want the agent to react.
            # SessionEventInput carries no response_id; this is a
            # pre-execution history seed, so tag all items with a
            # synthetic ``"seed"`` response id. The runner overwrites
            # this on first turn via a normal append path.
            new_items = [
                NewConversationItem(
                    type=item.type,
                    response_id="seed",
                    data=item.data,
                    created_by=_attribution_user(user_id),
                )
                for item in initial_items
            ]
            await asyncio.to_thread(conversation_store.append, conv.id, new_items)
        else:
            await _ensure_runner_session_initialized(conv.id, conv, runner_client)
            await _ensure_runner_relay_ready(
                conv.id,
                conv.runner_id,
                runner_client,
                conversation_store,
            )
            for item in initial_items:
                await _forward_event_to_runner(
                    conv.id,
                    conv,
                    item,
                    conversation_store,
                    runner_client,
                    agent_name=agent.name,
                    created_by=_attribution_user(user_id),
                )
    # Re-read rather than reusing the local ``conv``: the label-only branch
    # above and ``_forward_event_to_runner`` can mutate the row after it was
    # built, so a fresh read is what keeps the create response current.
    return await _get_session_snapshot(
        conversation_store,
        conv.id,
        agent_store=agent_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
    )

def _create_session_from_bundle(
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    bundle_bytes: bytes,
    runner_id: str | None = None,
    tenant_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Validate, store, and persist a bundled session request.

    Each upload creates a session-scoped agent row, even when a
    template agent with the same spec name already exists. Agent
    names are user-authored labels, not global content identities:
    reusing a template by name would make a fresh ``omnigent run
    <yaml>`` session execute whatever bundle that template currently
    points at, silently discarding the uploaded bundle and coupling
    unrelated users who chose the same name.

    :param agent_store: Store that owns agent definitions.
    :param conversation_store: Store that owns conversation/session rows.
    :param artifact_store: Store for uploaded bundle bytes.
    :param metadata: Validated session metadata. When
        ``metadata.parent_session_id`` is set (already authorized by
        the caller), the session is created as a sub-agent
        child of that conversation.
    :param bundle_bytes: Raw uploaded ``.tar.gz`` agent bundle.
    :param runner_id: Optional runner binding inherited from the
        parent session (caller-resolved, ownership-checked),
        e.g. ``"runner_abc123"``. ``None`` leaves the session
        unbound.
    :returns: Response with the new session id.
    :raises OmnigentError: If bundle validation or agent insert
        integrity checks fail, or the parent session vanished
        between authorization and insert.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    # Enforce the policy-handler allowlist only on a shared /
    # multi-user server. On a trusted single-user/local server,
    # ``omnigent run`` uploads the operator's own bundle through this same
    # path, so custom handlers must keep working (the operator already has
    # code execution — the restriction would add no security there).
    spec = validate_agent_bundle(
        bundle_bytes,
        enforce_handler_allowlist=not local_single_user_enabled(),
    )
    assert spec.name is not None

    agent_id = generate_agent_id()
    agent_bundle_location = bundle_location(agent_id, bundle_bytes)
    try:
        artifact_store.put(agent_bundle_location, bundle_bytes)
    except Exception:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise
    return _persist_stored_session_bundle(
        agent_store,
        conversation_store,
        artifact_store,
        metadata,
        agent_id=agent_id,
        agent_name=spec.name,
        agent_bundle_location=agent_bundle_location,
        agent_description=spec.description,
        runner_id=runner_id,
        tenant_id=tenant_id,
    )

