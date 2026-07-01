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

def register_session_delete(
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
        # ── DELETE /sessions/{session_id} ──────────────────────────────

        @router.delete(
            "/sessions/{session_id}",
            response_model=None,
        )
        async def delete_session(
            request: Request,
            session_id: str,
            delete_branch: bool = False,
        ) -> ConversationDeleted:
            """Delete a session and all associated resources.

            Requires owner-level access. Tears down tasks, runner-side
            resources (environments, terminals), session files, and the
            conversation row.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param delete_branch: Opt-in git cleanup, as a query param
                (``?delete_branch=true``). When ``True`` and the session
                has a server-created worktree (``git_branch`` set), the
                host removes the worktree directory and deletes its branch
                (``git worktree remove --force`` then ``git branch -D``).
                Ignored for sessions with no worktree. Best-effort: a
                cleanup failure does not block the delete. Defaults to
                ``False`` (worktree and branch left untouched). See
                designs/SESSION_GIT_WORKTREE.md.
            :returns: A :class:`ConversationDeleted` confirmation.
            :raises OmnigentError: 404 if no session or no access,
                403 if insufficient permissions.
            """
            user_id = _require_user(request, auth_provider)
            if permission_store is not None and user_id is not None:
                is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
                if not is_admin:
                    grant = await asyncio.to_thread(permission_store.get, user_id, session_id)
                    if grant is None or grant.level < LEVEL_OWNER:
                        if grant is not None:
                            raise OmnigentError(
                                "Only the session owner can delete this session",
                                code=ErrorCode.FORBIDDEN,
                            )
                        raise OmnigentError(
                            "Conversation not found",
                            code=ErrorCode.NOT_FOUND,
                        )
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
            # Runner-side resource cleanup is best-effort: if the bound
            # runner is offline or unbound, the session must still be
            # deletable. Server-owned records (files and conversation row
            # below) live independently of the runner, and runner-side
            # resources are gone with the runner anyway.
            runner_client: httpx.AsyncClient | None = None
            try:
                runner_client = await _get_runner_client_for_resource_access(session_id)
            except OmnigentError as exc:
                _logger.info(
                    "Skipping runner-side cleanup for %s; proceeding with server-side delete: %s",
                    session_id,
                    exc,
                )
            if runner_client is not None:
                try:
                    await runner_client.delete(
                        f"/v1/sessions/{session_id}/resources",
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Runner cleanup failed for %s, falling back",
                        session_id,
                    )
            else:
                import contextlib

                from omnigent.runtime import get_terminal_registry

                with contextlib.suppress(RuntimeError):
                    await get_terminal_registry().cleanup_conversation(session_id)
            # Session file cleanup.
            if file_store is not None and artifact_store is not None:
                deleted_file_ids = await asyncio.to_thread(
                    file_store.delete_all_for_session, session_id
                )
                for fid in deleted_file_ids:
                    await asyncio.to_thread(artifact_store.delete, fid)
            # Opt-in git worktree cleanup: only when delete_branch=true and
            # the session has a server-created worktree. Runs after runner
            # teardown; best-effort (designs/SESSION_GIT_WORKTREE.md).
            if (
                delete_branch
                and conv.git_branch is not None
                and conv.workspace is not None
                and conv.host_id is not None
            ):
                await _remove_session_worktree_best_effort(
                    host_id=conv.host_id,
                    worktree_path=conv.workspace,
                    branch=conv.git_branch,
                    delete_branch=True,
                    request=request,
                    reason="session-delete",
                )
            _interrupt_fenced_sessions.discard(session_id)
            deleted = await conversation_store.delete_conversation(session_id)
            if not deleted:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
            # The session is gone, so is its launch-progress state. Failed
            # launches are retained in the cache for reload visibility while
            # the session exists; without this eviction every deleted
            # failed-launch session would leak one entry for the process
            # lifetime.
            _session_sandbox_status_cache.pop(session_id, None)
            # BDP-2434: drop any stashed OBO subject_token for the deleted session.
            evict_subject_token(request.app.state, session_id)
            # Same for the tracker's entry — a deleted session's launch can
            # never be rendezvoused again (access checks 404 first), so a
            # retained failure is dead weight. ``finish`` also settles a
            # still-in-flight entry, releasing any parked message POST into
            # its session re-read (which now correctly 404s); the background
            # task's later ``fail`` on the popped entry is a no-op.
            managed_launches_for_delete = getattr(request.app.state, "managed_launches", None)
            if managed_launches_for_delete is not None:
                managed_launches_for_delete.finish(session_id)
            # Managed-host cleanup: when the session's host is backed by a
            # server-provisioned sandbox (host_type="managed"), terminate
            # the sandbox and delete the host row — which also revokes its
            # launch token. Best-effort by design — the provider's lifetime
            # cap reaps stragglers. External (laptop) hosts have no
            # sandbox_id and are never touched.
            host_store_for_managed = getattr(request.app.state, "host_store", None)
            if conv.host_id is not None and host_store_for_managed is not None:
                bound_host = await asyncio.to_thread(host_store_for_managed.get_host, conv.host_id)
                if bound_host is not None and bound_host.sandbox_id is not None:
                    from omnigent.server.managed_hosts import terminate_managed_host

                    await terminate_managed_host(
                        bound_host,
                        host_store_for_managed,
                        # Supplies the launcher for the provider-side
                        # terminate; None (config removed since launch)
                        # still deletes the row and revokes the token.
                        getattr(request.app.state, "sandbox_config", None),
                    )
            return ConversationDeleted(id=session_id)

        # ── Permission management endpoints ──────────────────────────

        @router.put(
            "/sessions/{session_id}/permissions",
            response_model=None,
        )
        async def grant_permission(
            request: Request,
            response: Response,
            session_id: str,
            body: GrantPermissionRequest,
        ) -> PermissionObject:
            """Grant or update a permission on a session.

            Requires manage-level access. Upserts the grant — can
            upgrade or downgrade an existing level. Auto-creates the
            grantee user if they don't exist yet.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session to grant access to,
                e.g. ``"conv_abc123"``.
            :param body: The grant request with ``user_id`` and ``level``.
            :returns: The resulting :class:`PermissionObject`.
            :raises OmnigentError: 404 if no session or no access,
                401 if unauthenticated.
            """
            user_id = _require_user(request, auth_provider)
            await _require_access(
                user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
            )
            if permission_store is None:
                raise OmnigentError(
                    "Permissions not enabled",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            if body.user_id == user_id:
                raise OmnigentError(
                    "Cannot modify your own permissions",
                    code=ErrorCode.FORBIDDEN,
                )
            if body.user_id == RESERVED_USER_PUBLIC and body.level > LEVEL_READ:
                raise OmnigentError(
                    "Public access is limited to read-only (level 1)",
                    code=ErrorCode.INVALID_INPUT,
                )
            existing = await asyncio.to_thread(permission_store.get, body.user_id, session_id)
            if existing is not None and existing.level == LEVEL_OWNER:
                raise OmnigentError(
                    "Cannot modify owner permissions",
                    code=ErrorCode.FORBIDDEN,
                )
            await asyncio.to_thread(permission_store.ensure_user, body.user_id)
            # If-Match optimistic concurrency (BDP-2412): when the caller sends the
            # grant version they read, the store does a guarded compare-and-swap.
            from omnigent.server.etag import parse_if_match

            expected_version = parse_if_match(request.headers.get("if-match"))
            perm = await asyncio.to_thread(
                permission_store.grant,
                body.user_id,
                session_id,
                body.level,
                expected_version=expected_version,
            )
            # Push the now-shared session to the GRANTEE's open tabs so it
            # appears in their sidebar without a list poll.
            _announce_session_added(body.user_id, session_id)
            response.headers["ETag"] = f'"{perm.version}"'
            return PermissionObject(
                user_id=perm.user_id,
                conversation_id=perm.conversation_id,
                level=perm.level,
                version=perm.version,
            )

        @router.delete(
            "/sessions/{session_id}/permissions/{target_user_id}",
            status_code=204,
            response_model=None,
        )
        async def revoke_permission(
            request: Request,
            session_id: str,
            target_user_id: str,
        ) -> Response:
            """Revoke a user's permission on a session.

            Requires manage-level access. Cannot revoke your own
            manage grant (prevents orphaned sessions). Returns 204
            whether or not the grant existed (idempotent).

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session to revoke access from,
                e.g. ``"conv_abc123"``.
            :param target_user_id: User whose grant to revoke,
                e.g. ``"alice@example.com"``.
            :returns: 204 No Content.
            :raises OmnigentError: 404 if no session or no access,
                403 if attempting to revoke own manage grant.
            """
            user_id = _require_user(request, auth_provider)
            await _require_access(
                user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
            )
            if permission_store is None:
                raise OmnigentError(
                    "Permissions not enabled",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            if target_user_id == user_id:
                raise OmnigentError(
                    "Cannot modify your own permissions",
                    code=ErrorCode.FORBIDDEN,
                )
            existing = await asyncio.to_thread(permission_store.get, target_user_id, session_id)
            if existing is not None and existing.level == LEVEL_OWNER:
                raise OmnigentError(
                    "Cannot revoke owner permissions",
                    code=ErrorCode.FORBIDDEN,
                )
            await asyncio.to_thread(permission_store.revoke, target_user_id, session_id)
            return Response(status_code=204)

        @router.get(
            "/sessions/{session_id}/owner",
            response_model=None,
        )
        async def get_session_owner(
            request: Request,
            session_id: str,
        ) -> dict[str, str | None]:
            """Return the owner of a session.

            Requires read-level access.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session to look up,
                e.g. ``"conv_abc123"``.
            :returns: ``{"owner": "<user_id>"}`` or
                ``{"owner": null}``.
            """
            user_id = _require_user(request, auth_provider)
            await _require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            return {"owner": _get_session_owner_id(session_id, permission_store)}

        @router.get(
            "/sessions/{session_id}/permissions",
            response_model=None,
        )
        async def list_permissions(
            request: Request,
            session_id: str,
        ) -> list[PermissionObject]:
            """List all permission grants on a session.

            Requires manage-level access.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session to list grants for,
                e.g. ``"conv_abc123"``.
            :returns: List of :class:`PermissionObject`.
            :raises OmnigentError: 404 if no session or no access.
            """
            user_id = _require_user(request, auth_provider)
            await _require_access(
                user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
            )
            if permission_store is None:
                raise OmnigentError(
                    "Permissions not enabled",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            grants = await asyncio.to_thread(permission_store.list_for_session, session_id)
            return [
                PermissionObject(
                    user_id=g.user_id,
                    conversation_id=g.conversation_id,
                    level=g.level,
                )
                for g in grants
            ]

        # ── Agent sub-resource ────────────────────────────────────────
        # These endpoints expose the session's bound agent metadata
        # and bundle through the session namespace, removing the need
        # for a standalone ``/api/agents`` router.

        def _policy_type(spec: PolicySpec) -> str:
            """Return ``"function"`` for all policies."""
            if isinstance(spec, FunctionPolicySpec):
                return "function"
            return "unknown"

        def _policy_description(spec: PolicySpec) -> str | None:
            """Return a short description for a policy spec.

            Looks up the policy registry for a human-readable
            description; falls back to the callable path.
            """
            if isinstance(spec, FunctionPolicySpec) and spec.function:
                from omnigent.policies.registry import get_entry

                entry = get_entry(spec.function.path)
                return entry.description if entry else spec.function.path
            return None

        def _to_agent_object(agent: Agent, cache: AgentCache | None) -> AgentObject:
            """
            Convert a runtime :class:`Agent` entity to an API-layer
            :class:`AgentObject`.

            Loads the agent spec from *cache* to populate ``mcp_servers``,
            ``policies``, ``skills``, and (when the stored row has none) the
            ``description``. If the cache is ``None``, the spec is not
            cached, or the load fails, those fall back to empty lists / the
            stored value rather than raising — the endpoint must not fail
            because one spec can't be read.

            :param agent: The runtime agent entity.
            :param cache: Agent cache, or ``None`` in test setups.
            :returns: An :class:`AgentObject` for the API response.
            """
            mcp_servers: list[MCPServerSummary] = []
            policies: list[PolicySummary] = []
            skills: list[SkillSummary] = []
            terminals: list[str] = []
            # Harness/kind for the UI; None until the spec loads (mirrors the
            # GET /v1/agents catalog so both endpoints report it consistently).
            harness: str | None = None
            # Human display name from the bundle's params.displayName, e.g.
            # "Maya Chen" — mirrors the GET /v1/agents projection so a
            # session-bound agent renders by its human name, not the slug.
            display_name: str | None = None
            # Prefer the stored entity's description; fall back to the spec's
            # top-level description when the stored value is unset (single-file
            # YAML agents don't persist it at registration today). Lets the
            # new-session picker show a hover description without a migration.
            description: str | None = agent.description
            if cache is not None:
                try:
                    loaded = cache.load(
                        agent.id, agent.bundle_location, expand_env=agent.session_id is None
                    )
                    harness = loaded.spec.executor.harness_kind
                    _params = loaded.spec.params or {}
                    if isinstance(_params, dict):
                        _dn = _params.get("displayName")
                        display_name = str(_dn) if _dn else None
                    if description is None:
                        description = loaded.spec.description
                    # Declared terminal names, in spec order — the Web UI
                    # gates its "new terminal" affordance on this list.
                    terminals = list(loaded.spec.terminals or {})
                    # Bundled skills only (mirrors GET /v1/agents); the merged
                    # bundled + host-discovered set lives on the session snapshot.
                    skills = [
                        SkillSummary(name=s.name, description=s.description)
                        for s in loaded.spec.skills
                    ]
                    mcp_servers = [
                        MCPServerSummary(
                            name=srv.name,
                            transport=srv.transport,
                            description=srv.description,
                            url=srv.url,
                            command=srv.command,
                            args=srv.args,
                        )
                        for srv in loaded.spec.mcp_servers
                    ]
                    if loaded.spec.guardrails and loaded.spec.guardrails.policies:
                        policies = [
                            PolicySummary(
                                name=ps.name,
                                type=_policy_type(ps),
                                on=[
                                    f"{sel.phase.value}:{sel.tool_name}"
                                    if sel.tool_name
                                    else sel.phase.value
                                    for sel in (ps.on or [])
                                ],
                                description=_policy_description(ps),
                            )
                            for ps in loaded.spec.guardrails.policies
                        ]
                except Exception:  # noqa: BLE001 — spec load failure must not break agent fetch
                    _logger.debug(
                        "Failed to load spec for agent %s; mcp_servers/policies will be empty",
                        agent.id,
                        exc_info=True,
                    )
            return AgentObject(
                id=agent.id,
                name=agent.name,
                display_name=display_name,
                version=agent.version,
                description=description,
                created_at=agent.created_at,
                updated_at=agent.updated_at,
                harness=harness,
                mcp_servers=mcp_servers,
                policies=policies,
                skills=skills,
                terminals=terminals,
            )

        @router.get("/sessions/{session_id}/agent")
        async def get_session_agent(
            request: Request,
            session_id: str,
        ) -> AgentObject:
            """
            Return the :class:`AgentObject` for the session's bound agent.

            Replaces the standalone ``GET /api/agents/{id}`` endpoint by
            resolving the agent through the session's ``agent_id`` foreign
            key. The caller only needs to know the session id.

            :param request: The incoming FastAPI request.
            :param session_id: Session identifier, e.g.
                ``"conv_abc123"``.
            :returns: The bound agent's :class:`AgentObject`.
            :raises OmnigentError: If the session or agent is not found.
            """
            user_id = _require_user(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = conversation_store.get_conversation(session_id)
                if conv is None:
                    raise OmnigentError(
                        f"Session not found: {session_id!r}",
                        code=ErrorCode.NOT_FOUND,
                    )
            if conv.agent_id is None:
                raise OmnigentError(
                    "Session has no agent binding",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is None:
                raise OmnigentError(
                    f"Agent not found: {conv.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
            return _to_agent_object(agent, agent_cache)

        @router.get(
            "/sessions/{session_id}/agent/contents",
            response_class=Response,
            responses={
                200: {"content": {"application/gzip": {}}},
                404: {"description": "Session or agent not found"},
            },
        )
        async def get_session_agent_contents(
            request: Request,
            session_id: str,
        ) -> Response:
            """
            Download the raw ``.tar.gz`` agent bundle for the session's
            bound agent.

            Replaces ``GET /api/agents/{id}/contents``. Runners call this
            on cache miss to fetch the spec + bundled files.

            :param request: The incoming FastAPI request.
            :param session_id: Session identifier, e.g.
                ``"conv_abc123"``.
            :returns: Raw bundle bytes as ``application/gzip``.
            :raises OmnigentError: If the session, agent, or bundle is
                not found.
            """
            user_id = _require_user(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = conversation_store.get_conversation(session_id)
                if conv is None:
                    raise OmnigentError(
                        f"Session not found: {session_id!r}",
                        code=ErrorCode.NOT_FOUND,
                    )
            if conv.agent_id is None:
                raise OmnigentError(
                    "Session has no agent binding",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is None:
                raise OmnigentError(
                    f"Agent not found: {conv.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
            if artifact_store is None:
                raise OmnigentError(
                    "Artifact store not configured",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            bundle_bytes = artifact_store.get(agent.bundle_location)
            if bundle_bytes is None:
                raise OmnigentError(
                    "Agent bundle not found in artifact store",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            return Response(
                content=bundle_bytes,
                media_type="application/gzip",
                headers={
                    "X-Agent-Version": str(agent.version),
                    "X-Agent-Name": agent.name,
                    # Provenance for the runner's env-expansion decision:
                    # session-scoped agents are
                    # tenant-uploaded and must NOT have ${VAR} expanded
                    # against the runner process env; template agents
                    # (session_id is None) are operator-authored and may.
                    # The runner fails safe (treats a missing header as
                    # session-scoped → no expansion).
                    "X-Agent-Session-Scoped": "true" if agent.session_id is not None else "false",
                },
            )

        @router.put(
            "/sessions/{session_id}/agent",
        )
        async def update_session_agent(
            request: Request,
            session_id: str,
            bundle: Annotated[UploadFile, File(...)],
        ) -> AgentObject:
            """
            Replace the session's agent bundle with a new upload.

            Validates the new bundle, checks that the spec name matches
            the existing agent, stores the bundle under a
            content-addressed key, updates the agent row, and warm-swaps
            the cache. Idempotent when the bundle content is unchanged.

            :param request: The incoming FastAPI request.
            :param session_id: Session identifier, e.g.
                ``"conv_abc123"``.
            :param bundle: Uploaded ``.tar.gz`` agent bundle file.
            :returns: The updated :class:`AgentObject`.
            :raises OmnigentError: If the session or agent is not found,
                the bundle is invalid, or the spec name doesn't match.
            """
            user_id = _require_user(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = conversation_store.get_conversation(session_id)
                if conv is None:
                    raise OmnigentError(
                        f"Session not found: {session_id!r}",
                        code=ErrorCode.NOT_FOUND,
                    )
            if conv.agent_id is None:
                raise OmnigentError(
                    "Session has no agent binding",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is None:
                raise OmnigentError(
                    f"Agent not found: {conv.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )

            bundle_bytes = await bundle.read()
            # Run bundle validation (tar extraction + spec parse, both
            # blocking) off the event loop -- mirrors the POST
            # /sessions/bundled path. A malicious bundle that blocks here
            # must not hang the entire server loop. The
            # policy-handler allowlist is enforced only on a
            # shared / multi-user server; a trusted single-user/local server
            # keeps supporting custom handlers (see _create_session_from_bundle).
            spec = await asyncio.to_thread(
                validate_agent_bundle,
                bundle_bytes,
                enforce_handler_allowlist=not local_single_user_enabled(),
            )
            if spec.name is None:
                raise OmnigentError("spec missing name", code=ErrorCode.INVALID_INPUT)

            if spec.name != agent.name:
                raise OmnigentError(
                    f"spec name '{spec.name}' does not match agent "
                    f"name '{agent.name}'; name is immutable",
                    code=ErrorCode.INVALID_INPUT,
                )

            # Store the bundle, repoint the row, and warm-swap the cache via
            # the shared helper (content-addressed + idempotent). Only
            # operator-authored template agents (session_id is None) may
            # expand ${VAR} against the server env; tenant session-scoped
            # bundles must not. Blocking IO → run off the event loop.
            updated = await asyncio.to_thread(
                apply_bundle_update,
                agent,
                bundle_bytes,
                artifact_store=artifact_store,
                agent_store=agent_store,
                agent_cache=agent_cache,
                expand_env=agent.session_id is None,
            )

            return _to_agent_object(updated, agent_cache)

