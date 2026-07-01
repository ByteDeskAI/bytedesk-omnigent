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

def register_session_list(
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
        # ── GET /sessions ───────────────────────────────────────────

        @router.get(
            "/sessions",
            response_model=None,
        )
        async def list_sessions(
            request: Request,
            limit: int = Query(default=20, ge=1, le=1000),
            after: str | None = Query(default=None),
            before: str | None = Query(default=None),
            agent_id: str | None = Query(default=None),
            agent_name: str | None = Query(default=None),
            order: str = Query(default="desc", pattern="^(asc|desc)$"),
            sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at)$"),
            search_query: str | None = Query(default=None),
            include_archived: bool = Query(default=False),
            kind: str = Query(default="default", pattern="^(default|sub_agent|any)$"),
        ) -> PaginatedList:
            """
            List sessions with cursor-based pagination.

            Sessions are conversations with a non-``None`` ``agent_id``
            — i.e. those created via ``POST /v1/sessions``.
            Conversations without an agent binding are excluded.

            :param limit: Maximum number of sessions to return
                (1-1000, default 20).
            :param after: Cursor — return sessions after this
                session ID in sort order, e.g. ``"conv_abc123"``.
            :param before: Cursor — return sessions before this
                session ID.
            :param agent_id: When set, only return sessions bound
                to this agent, e.g. ``"ag_abc123"``. ``None``
                returns sessions across all agents.
            :param agent_name: When set, only return sessions whose
                bound agent row has this name. This intentionally
                includes session-scoped agents that share a name but
                have distinct bundles. ``None`` disables the filter.
            :param order: Sort direction, ``"desc"`` (newest-first)
                or ``"asc"`` (oldest-first).
            :param sort_by: Column to sort on, ``"created_at"`` or
                ``"updated_at"``.
            :param search_query: Case-insensitive substring filter on
                the session title or conversation content. ``None``
                or empty string disables the filter. A session
                matches if its title contains the query or any of
                its conversation items' text does. Powers the
                sidebar's session search.
            :param include_archived: When ``False`` (default), archived
                sessions are omitted. When ``True``, archived sessions
                are returned alongside active ones (the sidebar groups
                them into an "Archived" section). Powers the sidebar's
                "Show archived" toggle.
            :param kind: Conversation kind to return. ``"default"``
                (the default) returns only top-level user-initiated
                sessions — the sidebar's view. ``"sub_agent"`` returns
                only sub-agent child sessions. ``"any"`` returns both;
                this lets the new-session agent picker discover agents
                that are only bound to sub-agent sessions (e.g. ones
                uploaded via ``sys_session_create``).
            :returns: A :class:`PaginatedList` of
                :class:`SessionListItem`.
            """
            # Empty-string normalization — the UI sends
            # ``?search_query=`` when the search box is cleared and
            # that should behave identically to the param being
            # absent. Keeping the store's contract crisp: ``None``
            # means "no filter", anything else means "search".
            #
            # require_user, not get_user_id: ``accessible_by=None`` below
            # means "no ACL filter", so an unauthenticated request slipping
            # through as None would list EVERY user's sessions. Fail closed
            # with 401 instead (user_id stays None only when auth is
            # disabled entirely — no auth_provider).
            user_id = _require_user(request, auth_provider)
            # BDP-2438: admins get the per-owner ACL relaxed so they see every
            # session (the BDP-2395 tenant filter below still scopes them to their
            # tenant). ``is_admin`` is resolved here — it was already computed below
            # for the per-row permission badge; reused now, not recomputed.
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if (permission_store is not None and user_id is not None)
                else False
            )
            normalized_agent_id = agent_id
            if agent_id:
                resolved_agent = await asyncio.to_thread(resolve_agent_ref, agent_store, agent_id)
                if resolved_agent is None:
                    return PaginatedList(data=[], first_id=None, last_id=None, has_more=False)
                normalized_agent_id = resolved_agent.id
            normalized_query = search_query if search_query else None
            page = await asyncio.to_thread(
                conversation_store.list_conversations,
                limit=limit,
                after=after,
                before=before,
                agent_id=normalized_agent_id,
                agent_name=agent_name,
                accessible_by=_session_list_accessible_by(user_id, is_admin=user_is_admin),
                has_agent_id=True,
                # The store treats ``None`` as "no kind filter"; the API
                # spells that ``kind=any`` to keep the param required-ish
                # and pattern-validated.
                kind=None if kind == "any" else kind,
                order=order,
                sort_by=sort_by,
                search_query=normalized_query,
                include_archived=include_archived,
            )
            # Cross-tenant isolation (BDP-2395): the owner ACL above filters by
            # user; layer the tenant dimension on top so a tenant-scoped principal
            # never sees another tenant's sessions in the list. ``None`` caller
            # tenant (single-org / local) leaves the listing unchanged.
            _principal = auth_provider.get_principal(request) if auth_provider else None
            _caller_tenant = _principal.tenant_id if _principal else None
            if _caller_tenant is not None:
                page.data = [conv for conv in page.data if conv.tenant_id == _caller_tenant]
            # list_conversations may return rows with agent_id=None for
            # legacy conversations; skip them before building the batch IDs.
            conv_ids = [conv.id for conv in page.data if conv.agent_id is not None]
            if not conv_ids:
                return PaginatedList(
                    data=[],
                    first_id=page.first_id,
                    last_id=page.last_id,
                    has_more=page.has_more,
                )
            # Batch-fetch permissions and agent names concurrently.
            # The tasks table has been removed — status comes exclusively from
            # the relay-fed ``_session_status_cache``.
            unique_agent_ids = list({c.agent_id for c in page.data if c.agent_id is not None})
            if permission_store is not None:
                perms_by_conv, agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                    asyncio.to_thread(permission_store.list_for_sessions, conv_ids),
                    asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                    asyncio.to_thread(
                        conversation_store.list_child_conversation_ids_by_parent,
                        conv_ids,
                    ),
                )
            else:
                agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                    asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                    asyncio.to_thread(
                        conversation_store.list_child_conversation_ids_by_parent,
                        conv_ids,
                    ),
                )
                perms_by_conv: dict[str, list[SessionPermission]] = {}
            # In-memory lookup — no I/O, so batching avoids re-acquiring
            # the index's lock per row but otherwise has no DB cost.
            pending_counts = pending_elicitations.counts_for(conv_ids)
            agent_display_names_by_id = await asyncio.to_thread(
                _agent_display_names_for, unique_agent_ids, agent_store, agent_cache
            )
            comments_fingerprints = await _comments_fingerprints_for(conv_ids, comment_store)
            items: list[SessionListItem] = [
                _build_session_list_item(
                    conv,
                    agent_names_by_id=agent_names_by_id,
                    agent_display_names_by_id=agent_display_names_by_id,
                    grants=perms_by_conv.get(conv.id, []),
                    user_id=user_id,
                    user_is_admin=user_is_admin,
                    permissions_enabled=permission_store is not None,
                    pending_count=pending_counts.get(conv.id, 0),
                    child_session_ids=child_ids_by_parent[conv.id],
                    comments_fingerprint=comments_fingerprints.get(conv.id),
                )
                for conv in page.data
                if conv.agent_id is not None
            ]
            # The list deliberately does NOT compute per-item liveness
            # (runner_online / host_online). No list consumer reads it: the
            # sidebar no longer surfaces connection state, and the only live
            # consumer — the open-session view — sources liveness from the
            # single-session snapshot, the WS stream, and the /health poll, not
            # from list rows. Skipping it here removes the session-connectivity
            # and hosts-table queries from every GET /v1/sessions.
            return PaginatedList(
                data=[item.model_dump(exclude_none=True) for item in items],
                first_id=page.first_id,
                last_id=page.last_id,
                has_more=page.has_more,
            )
