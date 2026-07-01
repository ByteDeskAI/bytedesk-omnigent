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

def register_session_resources(
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
        # ── GET /sessions/{session_id}/resources ─────────────────────

        @router.get(
            "/sessions/{session_id}/resources",
            response_model=SessionResourcePaginatedList,
            response_model_exclude_none=True,
        )
        async def list_session_resources(
            request: Request,
            session_id: str,
            # Shadows the ``type`` builtin deliberately: FastAPI maps the
            # parameter name to the wire query param, which is ``?type=``.
            type: str | None = Query(default=None),
        ) -> SessionResourcePaginatedList:
            """
            Return the runner-authoritative resource inventory for a session.

            Requires the session to be bound to a runner via
            ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise.
            The server validates the session exists, then proxies to the
            runner's ``GET /v1/sessions/{id}/resources`` endpoint. In
            unit-test / in-process setups with no runner router/client, the
            route falls back to adapting the local terminal registry.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param type: Optional resource-type filter, e.g.
                ``"environment"`` / ``"terminal"`` / ``"file"``. Forwarded
                to the runner (its registry applies it) and honored by the
                local-registry fallback and the file-store merge below.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
            page: SessionResourcePaginatedList | None = None
            try:
                runner_client = await _get_runner_client_for_resource_access(session_id)
                if (
                    runner_client is None
                    and conv.runner_id is not None
                    and conv.host_id is not None
                    and await _heal_session_runner(session_id, request)
                ):
                    runner_client = await _get_runner_client_for_resource_access(session_id)
                if runner_client is not None:
                    page = await _proxy_get_session_resources_to_runner(
                        runner_client, session_id, resource_type=type
                    )
            except OmnigentError as exc:
                # Eager session-open ``GET /resources`` against a dead runner must
                # self-heal, not 503-storm (BDP-2579 rung 1/3). Relaunch/fail-over,
                # then retry the proxy once; if it still can't reach a runner, fall
                # through to the local-registry/file-store view so the client gets a
                # benign (possibly empty) page instead of an error loop.
                if not _is_runner_unavailable_error(exc):
                    raise
                if await _heal_session_runner(session_id, request):
                    healed_client = await _get_runner_client_for_resource_access(session_id)
                    if healed_client is not None:
                        with contextlib.suppress(OmnigentError, HTTPException):
                            page = await _proxy_get_session_resources_to_runner(
                                healed_client, session_id, resource_type=type
                            )
            if page is None:
                from omnigent.entities.session_resources import (
                    list_session_resources_from_terminal_registry,
                )
                from omnigent.runtime import get_terminal_registry

                try:
                    local_registry = get_terminal_registry()
                except RuntimeError:
                    local_registry = None
                resource_page = list_session_resources_from_terminal_registry(
                    session_id,
                    local_registry,
                )
                # Mirror the runner's ``?type=`` semantics on the fallback so
                # both paths return the same shape for filtered queries.
                local_data = [
                    SessionResourceObject.model_validate(
                        session_resource_view_to_dict(resource),
                    )
                    for resource in resource_page.data
                    if type is None or resource.type == type
                ]
                page = SessionResourcePaginatedList(
                    data=local_data,
                    first_id=local_data[0].id if local_data else None,
                    last_id=local_data[-1].id if local_data else None,
                    has_more=resource_page.has_more,
                )

            # Files live in the server's file store, not on the runner, so a
            # ``type`` filter for non-file resources must skip the merge.
            if file_store is not None and type in (None, "file"):
                file_page = await asyncio.to_thread(
                    file_store.list,
                    session_id=session_id,
                    limit=1000,
                )
                for stored in file_page.data:
                    resource_dict = _stored_file_to_resource(
                        session_id,
                        stored,
                    )
                    page.data.append(
                        SessionResourceObject.model_validate(resource_dict),
                    )
                if page.data:
                    page.last_id = page.data[-1].id
                    if not page.first_id:
                        page.first_id = page.data[0].id

            return page

        # ── Phase 1b: typed resource collections & terminal lifecycle ──

        async def _validate_session(
            session_id: str,
            request: Request | None = None,
            required_level: int = LEVEL_READ,
        ) -> Conversation:
            """Validate session existence and enforce permission checks.

            :param session_id: Session/conversation identifier.
            :param request: The incoming FastAPI request (for auth).
                When ``None``, permission checks are skipped (internal
                calls only).
            :param required_level: Minimum permission level needed.
            :returns: The matching conversation.
            :raises OmnigentError: 401/403/404 on auth or access failure.
            """
            if request is not None:
                user_id = _get_user_id(request, auth_provider)
                access = await _require_access_and_level(
                    user_id,
                    session_id,
                    required_level,
                    permission_store,
                    conversation_store,
                )
                # _require_access_and_level already fetched the conversation for
                # non-admin callers — reuse it to avoid a second DB round-trip.
                if access.conversation is not None:
                    return access.conversation
            # Fallback: no-auth path, admin caller, or permissions disabled.
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
            return conv

        async def _proxy_with_runner_heal(
            session_id: str,
            request: Request | None,
            attempt: Callable[[], Awaitable[Any]],
        ) -> Any:
            """Run a runner-proxy ``attempt`` with dead-runner self-heal.

            Mirrors the ``list_session_resources`` heal+retry+graceful pattern
            (BDP-2579): when the attempt fails because the runner is unavailable
            (``RUNNER_UNAVAILABLE`` / ``httpx.ConnectError``), relaunch/fail-over
            the session's runner and retry ONCE. A heal that still can't reach a
            runner surfaces a clean ``RUNNER_UNAVAILABLE`` (the API error layer
            maps it to 503 and the heal already published the graceful
            ``terminal_pending`` reconnecting state) — never an unhandled 500.

            ``request`` is ``None`` only for internal callers that cannot heal
            (no FastAPI request to drive the relaunch); those propagate as before.

            :param session_id: Session/conversation identifier.
            :param request: The incoming FastAPI request (drives the heal), or
                ``None`` to skip healing.
            :param attempt: Zero-arg coroutine factory performing the proxied call.
            :returns: The attempt's result.
            """
            try:
                return await attempt()
            except (OmnigentError, httpx.ConnectError) as exc:
                if request is None or not _is_runner_unavailable_error(exc):
                    raise
                if not await _heal_session_runner(session_id, request):
                    raise OmnigentError(
                        "runner unavailable for resource access",
                        code=ErrorCode.RUNNER_UNAVAILABLE,
                    ) from exc
                return await attempt()

        async def _proxy_get_to_runner(
            session_id: str,
            path: str,
            params: dict[str, str] | None = None,
            *,
            request: Request | None = None,
        ) -> dict[str, Any]:
            """Proxy a GET request to the runner and return parsed JSON.

            :param session_id: Session/conversation identifier.
            :param path: Runner-relative URL path.
            :param params: Optional query params forwarded to the runner,
                e.g. ``{"order": "asc"}``. ``None`` sends no query string.
            :param request: The incoming FastAPI request — when set, a dead
                runner self-heals once before retrying (BDP-2579).
            :returns: Parsed JSON response body.
            :raises HTTPException: 502 on runner failure.
            """

            async def _attempt() -> dict[str, Any]:
                runner_client = await _get_runner_client_for_resource_access(
                    session_id,
                )
                if runner_client is None:
                    raise HTTPException(
                        status_code=502,
                        detail="no runner available for resource access",
                    )
                try:
                    resp = await runner_client.get(path, params=params, timeout=10.0)
                except httpx.HTTPError as exc:
                    # A dead runner (ConnectError) becomes RUNNER_UNAVAILABLE so the
                    # heal wrapper can relaunch+retry (BDP-2579); other transport
                    # errors stay a generic 502.
                    if _is_runner_unavailable_error(exc):
                        raise OmnigentError(
                            "runner resource endpoint unavailable",
                            code=ErrorCode.RUNNER_UNAVAILABLE,
                        ) from exc
                    raise HTTPException(
                        status_code=502,
                        detail="runner resource endpoint unavailable",
                    ) from exc
                if resp.status_code == 404:
                    raise OmnigentError(
                        resp.json().get("error", {}).get("message", "Resource not found"),
                        code=ErrorCode.NOT_FOUND,
                    )
                if resp.status_code != 200:
                    try:
                        body = resp.json()
                        error = body.get("error", {})
                        msg = error.get("message") or "runner resource endpoint failed"
                    except Exception:  # noqa: BLE001
                        msg = "runner resource endpoint failed"
                    raise HTTPException(status_code=502, detail=msg)
                return resp.json()

            return await _proxy_with_runner_heal(session_id, request, _attempt)

        async def _proxy_post_to_runner(
            session_id: str,
            path: str,
            body: dict[str, Any],
            *,
            request: Request | None = None,
        ) -> tuple[int, dict[str, Any]]:
            """Proxy a POST request to the runner and return status + JSON.

            :param session_id: Session/conversation identifier.
            :param path: Runner-relative URL path.
            :param body: JSON body to forward.
            :param request: The incoming FastAPI request — when set, a dead
                runner self-heals once before retrying (BDP-2579).
            :returns: Tuple of (status_code, parsed_json_body).
            :raises HTTPException: 502 on transport failure.
            """

            async def _attempt() -> tuple[int, dict[str, Any]]:
                runner_client = await _get_runner_client_for_resource_access(
                    session_id,
                )
                if runner_client is None:
                    raise HTTPException(
                        status_code=502,
                        detail="no runner available for resource access",
                    )
                try:
                    resp = await runner_client.post(
                        path,
                        json=body,
                        timeout=10.0,
                    )
                except httpx.HTTPError as exc:
                    # Dead runner → RUNNER_UNAVAILABLE for the heal wrapper; other
                    # transport errors stay a generic 502 (BDP-2579).
                    if _is_runner_unavailable_error(exc):
                        raise OmnigentError(
                            "runner resource endpoint unavailable",
                            code=ErrorCode.RUNNER_UNAVAILABLE,
                        ) from exc
                    raise HTTPException(
                        status_code=502,
                        detail="runner resource endpoint unavailable",
                    ) from exc
                return resp.status_code, resp.json()

            return await _proxy_with_runner_heal(session_id, request, _attempt)

        async def _proxy_delete_to_runner(
            session_id: str,
            path: str,
        ) -> tuple[int, dict[str, Any]]:
            """Proxy a DELETE request to the runner and return status + JSON.

            :param session_id: Session/conversation identifier.
            :param path: Runner-relative URL path.
            :returns: Tuple of (status_code, parsed_json_body).
            :raises HTTPException: 502 on transport failure.
            """
            runner_client = await _get_runner_client_for_resource_access(
                session_id,
            )
            if runner_client is None:
                raise HTTPException(
                    status_code=502,
                    detail="no runner available for resource access",
                )
            try:
                resp = await runner_client.delete(path, timeout=10.0)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail="runner resource endpoint unavailable",
                ) from exc
            return resp.status_code, resp.json()

        async def _proxy_put_to_runner(
            session_id: str,
            path: str,
            body: dict[str, Any],
        ) -> tuple[int, dict[str, Any]]:
            """Proxy a PUT request to the runner.

            :param session_id: Session/conversation identifier.
            :param path: Runner-relative URL path.
            :param body: JSON body to forward.
            :returns: Tuple of (status_code, parsed_json_body).
            :raises HTTPException: 502 on transport failure.
            """
            runner_client = await _get_runner_client_for_resource_access(
                session_id,
            )
            if runner_client is None:
                raise HTTPException(
                    status_code=502,
                    detail="no runner available for resource access",
                )
            try:
                resp = await runner_client.put(
                    path,
                    json=body,
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail="runner resource endpoint unavailable",
                ) from exc
            return resp.status_code, resp.json()

        async def _proxy_patch_to_runner(
            session_id: str,
            path: str,
            body: dict[str, Any],
        ) -> tuple[int, dict[str, Any]]:
            """Proxy a PATCH request to the runner.

            :param session_id: Session/conversation identifier.
            :param path: Runner-relative URL path.
            :param body: JSON body to forward.
            :returns: Tuple of (status_code, parsed_json_body).
            :raises HTTPException: 502 on transport failure.
            """
            runner_client = await _get_runner_client_for_resource_access(
                session_id,
            )
            if runner_client is None:
                raise HTTPException(
                    status_code=502,
                    detail="no runner available for resource access",
                )
            try:
                resp = await runner_client.patch(
                    path,
                    json=body,
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail="runner resource endpoint unavailable",
                ) from exc
            return resp.status_code, resp.json()

        # Typed collection routes registered BEFORE /{resource_id} so
        # "environments", "terminals", "files" are not captured as ids.

        @router.get(
            "/sessions/{session_id}/resources/environments",
            response_model=None,
        )
        async def list_session_environments(
            request: Request,
            session_id: str,
        ) -> dict[str, Any]:
            """
            Return only environment resources for a session.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :returns: ``PaginatedList`` of environment resources.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            path = f"/v1/sessions/{session_id}/resources/environments"
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/environments/{environment_id}",
            response_model=None,
        )
        async def get_session_environment(
            request: Request,
            session_id: str,
            environment_id: str,
        ) -> dict[str, Any]:
            """
            Return a single environment resource by id.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Opaque environment resource id,
                e.g. ``"default"``.
            :returns: The environment resource object.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}"
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/terminals",
            response_model=None,
        )
        async def list_session_terminals(
            request: Request,
            session_id: str,
        ) -> dict[str, Any]:
            """
            Return only terminal resources for a session.

            The runner endpoint's pagination params (``limit`` / ``after`` /
            ``before`` / ``order``) are forwarded from the incoming query
            string — without this, a client-requested ``order=asc`` (the web
            terminal tabs rely on creation order to keep the session's own
            terminal first) would be silently dropped and the runner's
            ``desc`` default would apply.

            :param request: The incoming FastAPI request (for auth and the
                forwarded query params).
            :param session_id: Session/conversation identifier.
            :returns: ``PaginatedList`` of terminal resources.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            path = f"/v1/sessions/{session_id}/resources/terminals"
            forwarded = {
                key: value
                for key, value in request.query_params.items()
                if key in ("limit", "after", "before", "order")
            }
            return await _proxy_get_to_runner(
                session_id, path, params=forwarded or None, request=request
            )

        @router.post(
            "/sessions/{session_id}/resources/terminals",
            response_model=None,
            # CSRF hardening: body is parsed via request.json(); require a JSON
            # Content-Type so a cross-site text/plain request can't reach it.
            dependencies=[Depends(require_json_content_type)],
        )
        async def create_session_terminal(
            session_id: str,
            request: Request,
        ) -> Any:
            """
            Launch or return an existing terminal resource.

            Preserves ``sys_terminal_launch`` idempotency: an
            already-running ``(terminal, session_key)`` returns the
            existing resource.

            User-initiated creates are gated on the agent's terminal
            access: the requested ``terminal`` must be one of the names
            declared in the agent spec's ``terminals:`` block. Native
            harness bootstrap requests (marked ``ensure_native_terminal``
            or ``bridge_inject_dir`` — the ``omnigent claude`` / ``codex``
            wrappers launching the session's own CLI terminal) are exempt:
            they launch undeclared names via the runner's
            synthesize-from-body path and predate the gate. The markers
            are client-controlled, so the exemption is narrowed to the
            exact shape those wrappers send — a registered native terminal
            name with ``session_key`` ``"main"`` — anything else carrying a
            marker still goes through the declared-name gate (it would
            otherwise be an arbitrary-terminal bypass).

            :param session_id: Session/conversation identifier.
            :param request: JSON body with ``terminal`` and
                ``session_key``.
            :returns: The terminal resource object.
            :raises OmnigentError: 400 when the requested terminal is not
                declared by the agent spec (or the agent has no
                ``terminals:`` block at all).
            """
            conv = await _validate_session(session_id, request, LEVEL_EDIT)
            body = await request.json()
            is_native_bootstrap = (
                bool(body.get("ensure_native_terminal") or body.get("bridge_inject_dir"))
                and native_coding_agent_for_terminal_name(body.get("terminal")) is not None
                and body.get("session_key") == "main"
            )
            if not is_native_bootstrap:
                spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
                declared = list(spec.terminals or {}) if spec is not None else []
                if body.get("terminal") not in declared:
                    raise OmnigentError(
                        (
                            f"Terminal {body.get('terminal')!r} is not declared by this "
                            f"agent. Terminals can only be created for agents whose spec "
                            f"declares them; this agent declares: {declared or 'none'}."
                        ),
                        code=ErrorCode.INVALID_INPUT,
                    )
            path = f"/v1/sessions/{session_id}/resources/terminals"
            status, payload = await _proxy_post_to_runner(
                session_id,
                path,
                body,
                request=request,
            )
            if status >= 400:
                error = payload.get("error", {})
                # OmnigentError derives http_status from code; pass the runner's code, not a status.
                raise OmnigentError(
                    error.get("message", f"Terminal launch failed (runner returned HTTP {status})"),
                    code=error.get("code", ErrorCode.INTERNAL_ERROR),
                )
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.created",
                resource_id=payload.get("id", ""),
                resource_type="terminal",
                conversation_store=conversation_store,
                resource=payload,
            )
            return payload

        @router.get(
            "/sessions/{session_id}/resources/terminals/{terminal_id}",
            response_model=None,
        )
        async def get_session_terminal(
            request: Request,
            session_id: str,
            terminal_id: str,
        ) -> dict[str, Any]:
            """
            Return a single terminal resource by id.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param terminal_id: Opaque terminal resource id.
            :returns: The terminal resource object.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.post(
            "/sessions/{session_id}/resources/terminals/{terminal_id}/transfer",
            response_model=None,
            # CSRF hardening: body is parsed via request.json(); require a JSON
            # Content-Type so a cross-site text/plain request can't reach it.
            dependencies=[Depends(require_json_content_type)],
        )
        async def transfer_session_terminal(
            request: Request,
            session_id: str,
            terminal_id: str,
        ) -> Any:
            """
            Move a terminal resource to another session without closing it.

            Used by native Claude ``/clear`` rotation: ownership changes
            from the previous conversation to the fresh one while the tmux
            pane keeps running.

            :param request: The incoming FastAPI request (for auth) with
                JSON body ``{"target_session_id": "conv_new"}``.
            :param session_id: Current owning session/conversation id,
                e.g. ``"conv_old"``.
            :param terminal_id: Opaque terminal resource id,
                e.g. ``"terminal_claude_main"``.
            :returns: The terminal resource object under the target session.
            """
            await _validate_session(session_id, request, LEVEL_EDIT)
            body = await request.json()
            target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
            if not isinstance(target_session_id, str) or not target_session_id:
                raise OmnigentError(
                    "'target_session_id' is required",
                    code=ErrorCode.INVALID_INPUT,
                )
            await _validate_session(target_session_id, request, LEVEL_EDIT)

            path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer"
            status, payload = await _proxy_post_to_runner(
                session_id,
                path,
                {"target_session_id": target_session_id},
                request=request,
            )
            if status == 404:
                error = payload.get("error", {})
                raise OmnigentError(
                    error.get("message", "Terminal not found"),
                    code=ErrorCode.NOT_FOUND,
                )
            if status == 409:
                error = payload.get("error", {})
                raise OmnigentError(
                    error.get("message", "Terminal transfer conflict"),
                    code=ErrorCode.INVALID_INPUT,
                )
            if status >= 400:
                error = payload.get("error", {})
                raise OmnigentError(
                    error.get("message", "Terminal transfer failed"),
                    code=error.get("code", "internal_error"),
                    http_status=status,
                )

            _publish_and_persist_resource_event(
                session_id,
                "session.resource.deleted",
                resource_id=terminal_id,
                resource_type="terminal",
                conversation_store=conversation_store,
            )
            _publish_and_persist_resource_event(
                target_session_id,
                "session.resource.created",
                resource_id=payload.get("id", ""),
                resource_type="terminal",
                conversation_store=conversation_store,
                resource=payload,
            )
            return payload

        @router.delete(
            "/sessions/{session_id}/resources/terminals/{terminal_id}",
            response_model=None,
        )
        async def delete_session_terminal(
            request: Request,
            session_id: str,
            terminal_id: str,
        ) -> Any:
            """
            Close a terminal resource.

            Delegates to ``TerminalRegistry.close()`` on the runner.
            Returns 404 for unknown terminals.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param terminal_id: Opaque terminal resource id.
            :returns: Deletion confirmation object.
            """
            await _validate_session(session_id, request, LEVEL_EDIT)
            path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
            status, payload = await _proxy_delete_to_runner(
                session_id,
                path,
            )
            if status == 404:
                error = payload.get("error", {})
                raise OmnigentError(
                    error.get("message", "Terminal not found"),
                    code=ErrorCode.NOT_FOUND,
                )
            if status >= 400:
                raise HTTPException(
                    status_code=502,
                    detail="runner terminal delete failed",
                )
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.deleted",
                resource_id=terminal_id,
                resource_type="terminal",
                conversation_store=conversation_store,
            )
            return payload

        # ── Phase 1c: session-scoped file endpoints ────────────────────

        @router.get(
            "/sessions/{session_id}/resources/files",
            response_model=None,
        )
        async def list_session_files(
            request: Request,
            session_id: str,
            limit: int = Query(default=20, ge=1, le=1000),
            after: str | None = Query(default=None),
            before: str | None = Query(default=None),
            order: str = Query(default="desc", pattern="^(asc|desc)$"),
        ) -> dict[str, Any]:
            """
            List files owned by a session.

            :param session_id: Session/conversation identifier.
            :param limit: Maximum number of files to return.
            :param after: Cursor file ID for forward pagination.
            :param before: Cursor file ID for backward pagination.
            :param order: Sort direction, ``"desc"`` or ``"asc"``.
            :returns: ``PaginatedList`` of session file resources.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            if file_store is None:
                raise HTTPException(
                    status_code=501,
                    detail="file store not configured",
                )
            page = file_store.list(
                session_id=session_id,
                limit=limit,
                after=after,
                before=before,
                order=order,
            )
            data = [_stored_file_to_resource(session_id, f) for f in page.data]
            return {
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            }

        @router.post(
            "/sessions/{session_id}/resources/files",
            status_code=201,
            response_model=None,
        )
        async def upload_session_file(
            request: Request,
            session_id: str,
            file: Annotated[UploadFile, File(...)],
        ) -> dict[str, Any]:
            """
            Upload a file into the session file namespace.

            Accepts the multipart upload shape used by session file resources.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param file: The uploaded file (multipart form data).
            :returns: The session file resource object.
            """
            await _validate_session(session_id, request, LEVEL_EDIT)
            if file_store is None or artifact_store is None:
                raise HTTPException(
                    status_code=501,
                    detail="file store not configured",
                )
            if not file.filename:
                raise OmnigentError(
                    "filename is required",
                    code=ErrorCode.INVALID_INPUT,
                )
            content = await file.read()
            from omnigent.runtime.content_resolver import (
                _resolve_content_type,
            )

            content_type = _resolve_content_type(
                file.content_type,
                file.filename,
            )
            stored = file_store.create(
                session_id=session_id,
                filename=file.filename,
                bytes=len(content),
                content_type=content_type,
            )
            artifact_store.put(stored.id, content)
            resource = _stored_file_to_resource(session_id, stored)
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.created",
                resource_id=stored.id,
                resource_type="file",
                conversation_store=conversation_store,
                resource=resource,
            )
            return resource

        @router.get(
            "/sessions/{session_id}/resources/files/{file_id}",
            response_model=None,
        )
        async def get_session_file(
            request: Request,
            session_id: str,
            file_id: str,
        ) -> dict[str, Any]:
            """
            Retrieve metadata for a session file resource.

            Verifies that ``file_id`` belongs to ``session_id``.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param file_id: Unique file identifier.
            :returns: The session file resource object.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            if file_store is None:
                raise HTTPException(
                    status_code=501,
                    detail="file store not configured",
                )
            stored = file_store.get(file_id, session_id=session_id)
            if stored is None:
                raise OmnigentError(
                    "File not found",
                    code=ErrorCode.NOT_FOUND,
                )
            return _stored_file_to_resource(session_id, stored)

        @router.get(
            "/sessions/{session_id}/resources/files/{file_id}/content",
            response_model=None,
        )
        async def get_session_file_content(
            request: Request,
            session_id: str,
            file_id: str,
        ) -> Response:
            """
            Download raw content of a session file resource.

            :param session_id: Session/conversation identifier.
            :param file_id: Unique file identifier.
            :returns: Response with file bytes and Content-Type.
            """

            await _validate_session(session_id, request, LEVEL_READ)
            if file_store is None or artifact_store is None:
                raise HTTPException(
                    status_code=501,
                    detail="file store not configured",
                )
            stored = file_store.get(file_id, session_id=session_id)
            if stored is None:
                raise OmnigentError(
                    "File not found",
                    code=ErrorCode.NOT_FOUND,
                )
            content = artifact_store.get(stored.id)
            media_type = mimetypes.guess_type(stored.filename)[0] or "application/octet-stream"
            # The filename and bytes are fully user-controlled. Serving the
            # content inline lets a browser navigating directly to this URL
            # render an uploaded ``evil.html`` as ``text/html`` and execute
            # its script in the server's own origin (stored XSS — acute on
            # the OSS/local server, which has no CSRF/apiproxy boundary).
            # Force a download with ``Content-Disposition: attachment`` and
            # disable MIME sniffing so the response cannot be reinterpreted
            # as an active type.
            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Content-Disposition": _attachment_disposition(stored.filename),
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @router.delete(
            "/sessions/{session_id}/resources/files/{file_id}",
            response_model=None,
        )
        async def delete_session_file(
            request: Request,
            session_id: str,
            file_id: str,
        ) -> dict[str, Any]:
            """
            Delete a session file resource and its artifact bytes.

            :param session_id: Session/conversation identifier.
            :param file_id: Unique file identifier.
            :returns: Deletion confirmation object.
            """
            await _validate_session(session_id, request, LEVEL_EDIT)
            if file_store is None or artifact_store is None:
                raise HTTPException(
                    status_code=501,
                    detail="file store not configured",
                )
            if not file_store.delete(file_id, session_id=session_id):
                raise OmnigentError(
                    "File not found",
                    code=ErrorCode.NOT_FOUND,
                )
            artifact_store.delete(file_id)
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.deleted",
                resource_id=file_id,
                resource_type="file",
                conversation_store=conversation_store,
            )
            return {
                "id": file_id,
                "object": "session.resource.deleted",
                "deleted": True,
            }

        # ── Phase 3: environment filesystem proxy endpoints ──────────

        async def _proxy_fs_response(
            session_id: str,
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            *,
            request: Request | None = None,
            required_level: int = LEVEL_EDIT,
            environment_id: str = "default",
            publish_invalidation: bool = True,
        ) -> Any:
            """Proxy a filesystem request to the runner.

            Translates runner error status codes into appropriate
            API-level exceptions.

            :param session_id: Session/conversation identifier.
            :param method: HTTP method.
            :param path: Runner-relative URL path.
            :param body: Optional JSON body.
            :param request: The incoming FastAPI request (for auth).
            :param required_level: Minimum permission level needed.
            :param environment_id: Environment resource id,
                e.g. ``"default"``. Used for the live invalidation event
                after successful mutating filesystem operations.
            :param publish_invalidation: Whether a successful proxied
                mutation should publish ``session.changed_files.invalidated``.
                False for generic shell commands because read-only commands
                are common and cannot be distinguished cheaply here.
            :returns: Parsed JSON response.
            """
            await _validate_session(session_id, request, required_level)
            if method == "GET":
                return await _proxy_get_to_runner(session_id, path, request=request)
            if method == "PUT":
                status, payload = await _proxy_put_to_runner(
                    session_id,
                    path,
                    body or {},
                )
            elif method == "PATCH":
                status, payload = await _proxy_patch_to_runner(
                    session_id,
                    path,
                    body or {},
                )
            elif method == "POST":
                status, payload = await _proxy_post_to_runner(
                    session_id,
                    path,
                    body or {},
                    request=request,
                )
            elif method == "DELETE":
                status, payload = await _proxy_delete_to_runner(
                    session_id,
                    path,
                )
            else:
                raise HTTPException(status_code=405)

            if status >= 400:
                error = payload.get("error", {})
                message = error.get("message", "filesystem operation failed")
                if status == 404:
                    raise OmnigentError(message, code=ErrorCode.NOT_FOUND)
                raise HTTPException(status_code=status, detail=message)
            if publish_invalidation:
                _publish_changed_files_invalidated(session_id, environment_id)
            return payload

        @router.get(
            "/sessions/{session_id}/resources/environments/{environment_id}/filesystem",
            response_model=None,
        )
        async def list_environment_root(
            request: Request,
            session_id: str,
            environment_id: str,
            limit: int = Query(default=20, ge=1, le=1000),
            after: str | None = Query(default=None),
            before: str | None = Query(default=None),
            order: str = Query(default="desc", pattern="^(asc|desc)$"),
        ) -> Any:
            """
            List root directory of an environment.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param limit: Maximum number of entries to return (1-1000, default 20).
            :param after: Cursor entry id for forward pagination.
            :param before: Cursor entry id for backward pagination.
            :param order: Sort order, ``"asc"`` or ``"desc"``.
            :returns: PaginatedList of filesystem entries.
            """
            params: dict[str, str] = {"limit": str(limit), "order": order}
            if after is not None:
                params["after"] = after
            if before is not None:
                params["before"] = before
            qs = urllib.parse.urlencode(params)
            path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem?{qs}"
            await _validate_session(session_id, request, LEVEL_READ)
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/environments/{environment_id}/search",
            response_model=None,
        )
        async def search_environment_files(
            request: Request,
            session_id: str,
            environment_id: str,
            q: str = Query(min_length=1, pattern=r".*\S.*"),
            include: str | None = Query(default=None),
            exclude: str | None = Query(default=None),
            limit: int = Query(default=500, ge=1, le=500),
        ) -> Any:
            """
            Search for files recursively by name/path substring and glob filters.

            Proxies to the runner's search endpoint.  Returns a flat list of
            matching file entries (not directories) whose name or relative path
            contains ``q`` (case-insensitive), optionally scoped by ``include`` /
            ``exclude`` globs.  Requires at least one non-whitespace character in
            ``q`` to prevent accidental full-tree scans.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param environment_id: Environment resource id,
                e.g. ``"default"``.
            :param q: Case-insensitive search substring, e.g. ``"test.md"``.
                Must contain at least one non-whitespace character.
            :param include: Comma-separated glob patterns scoping which files are
                returned, e.g. ``"*.ts,src/**"``.
            :param exclude: Comma-separated glob patterns for files to drop,
                e.g. ``"**/node_modules,*.test.ts"``.
            :param limit: Maximum number of results (1-500, default 500).
            :returns: JSON list response with matching filesystem entries.
            """
            params: dict[str, str] = {"q": q, "limit": str(limit)}
            if include is not None:
                params["include"] = include
            if exclude is not None:
                params["exclude"] = exclude
            qs = urllib.parse.urlencode(params)
            path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/search?{qs}"
            await _validate_session(session_id, request, LEVEL_READ)
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/environments/{environment_id}/changes",
            response_model=None,
        )
        async def list_environment_filesystem_changes(
            request: Request,
            session_id: str,
            environment_id: str,
        ) -> Any:
            """
            List all files changed since session start (flat, registry-backed).

            Returns the watchdog change set for the session — every file
            created, modified, or deleted since the session began, regardless
            of directory depth.  Use for the flat "changed files" view.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :returns: Flat list of changed filesystem entries with ``status``.
            """
            path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/changes"
            await _validate_session(session_id, request, LEVEL_READ)
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/environments/{environment_id}/diff/{relative_path:path}",
            response_model=None,
        )
        async def read_environment_file_diff(
            request: Request,
            session_id: str,
            environment_id: str,
            relative_path: str,
        ) -> Any:
            """
            Return before/after diff content for a changed file.

            Proxies to the runner's diff endpoint and returns before/after
            content strings so the UI can render a diff view.  Returns 404 when
            the file has not been modified this session.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param relative_path: Path relative to environment root.
            :returns: JSON with ``before`` and ``after`` content strings.
            """
            path = (
                f"/v1/sessions/{session_id}/resources/environments"
                f"/{environment_id}/diff/{relative_path}"
            )
            await _validate_session(session_id, request, LEVEL_READ)
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.get(
            "/sessions/{session_id}/resources/environments"
            "/{environment_id}/filesystem/{relative_path:path}",
            response_model=None,
        )
        async def read_or_list_environment_path(
            request: Request,
            session_id: str,
            environment_id: str,
            relative_path: str,
            limit: int = Query(default=20, ge=1, le=1000),
            after: str | None = Query(default=None),
            before: str | None = Query(default=None),
            order: str = Query(default="desc", pattern="^(asc|desc)$"),
        ) -> Any:
            """
            Read a file or list a directory in an environment.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param relative_path: Path relative to environment root.
            :param limit: Maximum number of entries to return for directory
                listings (1-1000, default 20). Ignored for file reads.
            :param after: Cursor entry id for forward pagination.
            :param before: Cursor entry id for backward pagination.
            :param order: Sort order, ``"asc"`` or ``"desc"``.
            :returns: File content or directory listing.
            """
            params: dict[str, str] = {"limit": str(limit), "order": order}
            if after is not None:
                params["after"] = after
            if before is not None:
                params["before"] = before
            qs = urllib.parse.urlencode(params)
            path = (
                f"/v1/sessions/{session_id}/resources/environments"
                f"/{environment_id}/filesystem/{relative_path}?{qs}"
            )
            await _validate_session(session_id, request, LEVEL_READ)
            return await _proxy_get_to_runner(session_id, path, request=request)

        @router.put(
            "/sessions/{session_id}/resources/environments"
            "/{environment_id}/filesystem/{relative_path:path}",
            response_model=None,
        )
        async def write_environment_file(
            session_id: str,
            environment_id: str,
            relative_path: str,
            request: Request,
        ) -> Any:
            """
            Write/replace a file in an environment.

            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param relative_path: Path relative to environment root.
            :param request: JSON body with ``content``.
            :returns: Write result.
            """
            body = await request.json()
            path = (
                f"/v1/sessions/{session_id}/resources/environments"
                f"/{environment_id}/filesystem/{relative_path}"
            )
            return await _proxy_fs_response(
                session_id,
                "PUT",
                path,
                body,
                request=request,
                environment_id=environment_id,
            )

        @router.patch(
            "/sessions/{session_id}/resources/environments"
            "/{environment_id}/filesystem/{relative_path:path}",
            response_model=None,
        )
        async def edit_environment_file(
            session_id: str,
            environment_id: str,
            relative_path: str,
            request: Request,
        ) -> Any:
            """
            Edit a file in an environment via text replacement.

            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param relative_path: Path relative to environment root.
            :param request: JSON body with ``old_text`` and ``new_text``.
            :returns: Edit result.
            """
            body = await request.json()
            path = (
                f"/v1/sessions/{session_id}/resources/environments"
                f"/{environment_id}/filesystem/{relative_path}"
            )
            return await _proxy_fs_response(
                session_id,
                "PATCH",
                path,
                body,
                request=request,
                environment_id=environment_id,
            )

        @router.delete(
            "/sessions/{session_id}/resources/environments"
            "/{environment_id}/filesystem/{relative_path:path}",
            response_model=None,
        )
        async def delete_environment_path(
            request: Request,
            session_id: str,
            environment_id: str,
            relative_path: str,
        ) -> Any:
            """
            Delete a file or directory in an environment.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param relative_path: Path relative to environment root.
            :returns: Delete result.
            """
            path = (
                f"/v1/sessions/{session_id}/resources/environments"
                f"/{environment_id}/filesystem/{relative_path}"
            )
            return await _proxy_fs_response(
                session_id,
                "DELETE",
                path,
                request=request,
                environment_id=environment_id,
            )

        # ── Phase 5: environment shell proxy ─────────────────────────

        @router.post(
            "/sessions/{session_id}/resources/environments/{environment_id}/shell",
            response_model=None,
            # CSRF hardening: body is parsed via request.json(); require a JSON
            # Content-Type so a cross-site text/plain request can't reach it.
            dependencies=[Depends(require_json_content_type)],
        )
        async def run_environment_shell(
            session_id: str,
            environment_id: str,
            request: Request,
        ) -> Any:
            """
            Execute a shell command in an environment.

            :param session_id: Session/conversation identifier.
            :param environment_id: Environment resource id.
            :param request: JSON body with ``command`` and optional
                ``timeout``.
            :returns: Shell result.
            """
            body = await request.json()
            path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/shell"
            return await _proxy_fs_response(
                session_id,
                "POST",
                path,
                body,
                request=request,
                environment_id=environment_id,
                publish_invalidation=False,
            )

        # Generic single-resource lookup — registered AFTER typed
        # collections so "environments", "terminals", "files" are not
        # captured as resource_id.

        @router.get(
            "/sessions/{session_id}/resources/{resource_id}",
            response_model=None,
        )
        async def get_session_resource(
            request: Request,
            session_id: str,
            resource_id: str,
        ) -> dict[str, Any]:
            """
            Return a single resource by id from the unified inventory.

            :param session_id: Session/conversation identifier.
            :param resource_id: Opaque resource id.
            :returns: The resource object regardless of type.
            """
            await _validate_session(session_id, request, LEVEL_READ)
            path = f"/v1/sessions/{session_id}/resources/{resource_id}"
            return await _proxy_get_to_runner(session_id, path, request=request)

