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

async def _forward_approval_to_runner(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
) -> None:
    """
    Forward an approval verdict to the session's bound runner.

    Runner-side elicitations (policy approvals parked in the runner's
    ``_pending_approvals`` dict, scaffold dispatch) resolve when the
    canonical ``approval`` event reaches the runner's ``/events``. The
    server↔runner contract stays the ``approval`` event regardless of
    how the verdict arrived at the server (resolve URL or approval
    event). No-op when no runner is bound (in-process setups). HTTP
    errors are logged, not raised — a dead runner must not fail the
    caller's resolution (the server-side Future was already set).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param data: The approval payload to forward verbatim as the
        event ``data``, e.g. ``{"elicitation_id": "elicit_abc",
        "action": "accept"}``.
    :param runner_router: Router used to resolve the bound runner, or
        ``None`` in in-process setups (forward skipped).
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _APPROVAL_TYPE, "data": data},
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Approval forward failed for %r",
            session_id,
        )

async def _authorize_bundled_parent_and_inherit_runner(
    parent_session_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> str | None:
    """
    Authorize a bundled create's parent link and resolve runner affinity.

    The caller must have READ access to the parent session
    before inheriting anything, mirroring the JSON create path —
    without this, a forged parent link lets the caller inherit runner
    bindings and parent a session they don't control. On success the
    parent's runner binding is inherited (sub-agent co-location),
    subject to a defense-in-depth ownership check: a runner the
    caller doesn't own is not inherited.

    :param parent_session_id: The requested parent session id,
        e.g. ``"conv_abc123"``.
    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``.
    :param permission_store: Permission store for the access
        check; ``None`` in single-user / no-auth mode.
    :param conversation_store: Store for the parent-conversation read.
    :param runner_router: Router for the runner-ownership check;
        ``None`` skips it.
    :returns: The inherited runner id, or ``None`` when the parent has
        no runner binding or ownership disallows inheritance.
    :raises OmnigentError: 403/404 when the caller may not access the
        parent session.
    """
    await _require_access(
        user_id,
        parent_session_id,
        LEVEL_READ,
        permission_store,
        conversation_store,
    )
    parent_conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        parent_session_id,
    )
    if parent_conv is None:
        return None
    inherited_runner_id = parent_conv.runner_id
    if inherited_runner_id is not None and user_id is not None and runner_router is not None:
        runner_owner = runner_router.runner_owner(inherited_runner_id)
        if runner_owner is not None and runner_owner != user_id:
            return None
    return inherited_runner_id

async def _notify_runner_of_bundled_child(
    session_id: str,
    agent_id: str,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Notify the inherited runner that a bundled child session exists.

    Lets the runner initialize per-session state (inbox queue,
    agent-id cache) before the first forwarded event, mirroring the
    JSON create path's post-create notify. Failures are logged and
    swallowed — the notify is additive and must not fail the create.

    :param session_id: The new child session id, e.g. ``"conv_abc123"``.
    :param agent_id: The child's session-scoped agent id,
        e.g. ``"ag_abc123"``.
    :param runner_router: Router used to resolve the bound runner's
        client; ``None`` falls back to the in-process runner.
    :returns: None.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": agent_id,
                "sub_agent_name": None,
            },
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "Failed to notify runner about bundled session %s",
            session_id,
            exc_info=True,
        )

