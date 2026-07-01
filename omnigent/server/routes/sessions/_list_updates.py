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

def _discovery_key(user_id: str | None) -> str:
    """
    Map an (optional) user id to the :mod:`user_session_stream` channel key.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode.
    :returns: ``user_id`` when set, else :data:`_SHARED_DISCOVERY_KEY`.
    """
    return user_id if user_id is not None else _SHARED_DISCOVERY_KEY

def _announce_session_added(user_id: str | None, session_id: str) -> None:
    """
    Push a ``session_added`` discovery event to a user's updates streams.

    Called after a session becomes accessible to ``user_id`` (created, forked,
    or shared) so that user's open tabs surface it without a list poll. A no-op
    when the user has no stream connected.

    :param user_id: The user the session is now accessible to (the owner on
        create/fork, the grantee on share), or ``None`` in single-user mode.
    :param session_id: The newly-accessible session id, e.g. ``"conv_abc123"``.
    """
    user_session_stream.publish(
        _discovery_key(user_id), {"type": "session_added", "session_id": session_id}
    )
    # Typed consumer event-subscription seam (BDP-2394, ADR-0149): mirror the
    # discovery push as a typed Event Message on the per-user event hub, so a
    # consumer watching GET /v1/events (optionally filtered by type) observes
    # session lifecycle without hand-wiring a callback. Best-effort fan-out;
    # no-op when nobody is subscribed.
    event_hub.publish(
        _discovery_key(user_id),
        {"type": "session.created", "session_id": session_id},
    )

def _session_list_accessible_by(user_id: str | None, *, is_admin: bool) -> str | None:
    """The owner-ACL filter (``accessible_by``) for the session list (BDP-2438).

    A non-admin is scoped to sessions they own (``user_id``). An **admin** gets
    the per-owner ACL relaxed (``None`` = no owner filter) so they see every
    session — Office-driven sessions are owned by the ``local``/synthetic
    principal Office sends, not the logged-in admin, so without this an operator
    never sees them in the UI even though the agent replies arrive. The caller
    still layers the BDP-2395 tenant filter on top, so a tenant-scoped admin
    sees only their own tenant; a tenant-less (single-org / local) admin sees
    all. ``user_id`` of ``None`` (auth disabled) already means "no filter" and
    is returned unchanged.

    :param user_id: The authenticated caller, or ``None`` when auth is disabled.
    :param is_admin: Whether the caller is an admin.
    :returns: The value to pass as ``accessible_by`` to ``list_conversations``.
    """
    return None if is_admin else user_id

def _build_session_list_item(
    conv: Conversation,
    *,
    agent_names_by_id: dict[str, str | None],
    agent_display_names_by_id: dict[str, str | None],
    grants: list[SessionPermission],
    user_id: str | None,
    user_is_admin: bool,
    permissions_enabled: bool,
    pending_count: int,
    child_session_ids: list[str],
    comments_fingerprint: CommentsFingerprint | None,
) -> SessionListItem:
    """
    Assemble one :class:`SessionListItem` from a conversation row and
    pre-fetched batch data.

    Single source of truth for the list-item shape, shared by the
    ``GET /v1/sessions`` page builder and the ``WS /v1/sessions/updates``
    push stream so the two never drift. The caller is responsible for
    batching the permission grants, agent names, and pending-elicitation
    counts across the whole set and passing the per-conversation slice
    here.

    :param conv: The persisted conversation entity. Must have a
        non-``None`` ``agent_id`` (i.e. be a session, not a plain
        conversation) — the caller filters these out beforehand.
    :param agent_names_by_id: Map from agent id to slug name, as
        returned by ``agent_store.get_names()``,
        e.g. ``{"ag_abc": "research-agent"}``.
    :param agent_display_names_by_id: Map from agent id to human display
        name (``params.displayName``), as returned by
        :func:`_agent_display_names_for`, e.g. ``{"ag_abc": "Maya Chen"}``.
        Missing/``None`` when the bundle sets none — the client then
        falls back to the slug.
    :param grants: All permission grants for this conversation, as
        returned by ``permission_store.list_for_sessions()[conv.id]``.
        Empty list when permissions are disabled.
    :param user_id: The authenticated requesting user, or ``None`` when
        unauthenticated / permissions disabled,
        e.g. ``"alice@example.com"``.
    :param user_is_admin: Whether ``user_id`` holds the admin flag, from
        a single ``permission_store.is_admin()`` call made once for the
        whole batch.
    :param permissions_enabled: ``True`` when a permission store is
        wired; gates owner/level population to mirror ``list_sessions``.
    :param pending_count: Number of outstanding elicitations for this
        conversation, from ``pending_elicitations.counts_for()``.
    :param child_session_ids: Direct sub-agent children for this
        conversation, as returned by
        ``conversation_store.list_child_conversation_ids_by_parent()``.
    :param comments_fingerprint: Change-detection summary of this
        conversation's review comments, from
        ``comment_store.get_comments_fingerprints()[conv.id]``. ``None``
        when the conversation has no comments or no comment store is
        wired — emitted as ``comments_count=0`` /
        ``comments_updated_at=None`` so the two states look identical
        on the wire.
    :returns: The assembled :class:`SessionListItem`.
    """
    # ``conv.agent_id`` is guaranteed non-None by the caller (sessions
    # only); assert for the type checker without a runtime branch.
    assert conv.agent_id is not None
    level = _permission_level_from_grants(user_id, grants, user_is_admin)
    owner = _owner_from_grants(grants) if permissions_enabled else None
    return SessionListItem(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_names_by_id.get(conv.agent_id),
        agent_display_name=agent_display_names_by_id.get(conv.agent_id),
        status=_session_status_with_child_rollup(conv.id, child_session_ids),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        labels=labels_with_closed_status(conv.labels, conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        reasoning_effort=conv.reasoning_effort,
        permission_level=level,
        owner=owner,
        external_session_id=conv.external_session_id,
        pending_elicitations_count=pending_count,
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        comments_count=comments_fingerprint.count if comments_fingerprint else 0,
        comments_updated_at=(
            comments_fingerprint.last_updated_at if comments_fingerprint else None
        ),
    )

async def _apply_liveness_to_items(
    items: list[SessionListItem],
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
) -> None:
    """
    Attach runner + host liveness to session-list items when a lookup is
    wired.

    Both ``GET /v1/sessions`` and ``WS /v1/sessions/updates`` use this so
    HTTP reconciliation preserves the same ``runner_online`` /
    ``host_online`` fields that push frames patch into the web cache.

    :param items: Session-list rows to annotate.
    :param liveness_lookup: Bulk liveness lookup from session id to a
        :class:`SessionLiveness` pair, e.g.
        ``{"conv_abc123": SessionLiveness(runner_online=True,
        host_online=None)}``. ``None`` means this server cannot compute
        liveness for list rows, in which case both fields are left
        ``None``.
    :returns: ``None``. Mutates ``items`` in place.
    """
    if liveness_lookup is None or not items:
        return
    liveness = await asyncio.to_thread(liveness_lookup, [item.id for item in items])
    for item in items:
        result = liveness[item.id]
        item.runner_online = result.runner_online
        item.host_online = result.host_online

