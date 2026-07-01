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

async def _proxy_get_session_resources_to_runner(
    runner_client: httpx.AsyncClient,
    session_id: str,
    resource_type: str | None = None,
) -> SessionResourcePaginatedList:
    """Proxy ``GET /resources`` to the runner with strict validation.

    :param runner_client: HTTP client bound to the session's runner.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_type: Optional ``?type=`` filter forwarded to the
        runner, e.g. ``"environment"``. ``None`` returns all types.
    :returns: The runner's validated resource page.
    :raises HTTPException: 502 on runner failure or malformed response.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/resources",
            # Runner-side list_session_resources applies the type filter.
            params={"type": resource_type} if resource_type else None,
            timeout=10.0,
        )
        if resp.status_code != 200:
            _logger.warning(
                "session resources: runner returned %d for session=%s",
                resp.status_code,
                session_id,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint failed",
            )

        try:
            body = resp.json()
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            page = SessionResourceListPage.model_validate(body)
        except (TypeError, ValueError, ValidationError) as exc:
            _logger.warning(
                "session resources: malformed runner response for session=%s: %s",
                session_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint returned malformed response",
            ) from exc

        return SessionResourcePaginatedList(
            data=page.data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        _logger.warning(
            "session resources: runner call failed for session=%s (%s)",
            session_id,
            exc,
        )
        # A dead runner (ConnectError, the uniform BDP-2579 F2 signal) is
        # surfaced as RUNNER_UNAVAILABLE so the read path can self-heal instead
        # of 502-storming; other HTTP errors stay a generic 502.
        if _is_runner_unavailable_error(exc):
            raise OmnigentError(
                "runner session-resources endpoint unavailable",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        raise HTTPException(
            status_code=502,
            detail="runner session-resources endpoint unavailable",
        ) from exc

async def _reset_runner_resources_after_switch(session_id: str) -> None:
    """Best-effort reset of the session's runner-side state after a switch.

    Run as a fire-and-forget background task by the switch-agent route. Calls
    the runner's dedicated ``POST /v1/sessions/{id}/reset-state`` endpoint,
    which closes the cached primary OSEnv + terminals AND drops the
    spec-derived session caches. Two reasons:

    1. **Sandbox correctness.** The primary OSEnv (which backs the web-UI
       filesystem / shell endpoints) is materialized once per session from the
       *original* agent's spec and cached. Closing it AND invalidating the
       spec/snapshot caches forces the next access to re-resolve and
       re-materialize from the NEW agent's spec, so those endpoints run
       under the switched-to agent's ``os_env``/sandbox — not the old one.
       (Agent ``sys_os_*`` tool calls already re-derive os_env per call, and
       native terminals re-evaluate the sandbox gate on respawn; this closes
       the remaining stale path.)
    2. **Terminal rebuild.** A lingering native terminal would otherwise shadow
       the switch-back transcript rebuild (auto-create skips while one exists).

    A dedicated endpoint (rather than ``DELETE /resources``) keeps the
    session-deletion contract untouched — deletion never needs the
    switch-specific cache reset.

    A switch only runs while the session is idle, so closing the env + terminal
    here is safe — unlike doing it inside the next turn's dispatch, which wedges
    that turn. cwd is re-derived from the runner's bound workspace, so the
    working directory / git worktree is preserved (only the sandbox changes;
    a ``fork``/``start_in_scratch`` agent gets a fresh scratch copy). The
    claude-native auto-create gate remains the switch-back safety net if this
    call is lost (runner offline, races).

    :param session_id: Session/conversation id just switched, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is None:
            return
        reset_resp = await runner_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/reset-state",
            timeout=15.0,
        )
        # httpx only raises on transport errors — a 4xx/5xx reset response
        # still returns. A non-2xx means the runner did NOT close the old
        # env, so it must take the failure path below (suppressing the
        # invalidation publish); HTTPStatusError is an httpx.HTTPError.
        reset_resp.raise_for_status()
    except (httpx.HTTPError, HTTPException, OmnigentError, RuntimeError):
        # Best-effort: a runner hiccup must not break the (already-committed)
        # switch. OmnigentError covers the session-not-runner-bound / runner-
        # offline case raised by _get_runner_client_for_resource_access. The
        # auto-create gate rebuilds on switch-back regardless. No
        # changed-files event on this path either: the runner's env cache is
        # still the OLD agent's, so a triggered refetch would re-serve it —
        # and a lost runner rebuilds from the new spec on relaunch anyway.
        _logger.warning(
            "post-switch runner-resource reset failed for session=%s", session_id, exc_info=True
        )
        return
    # The old agent's cached OSEnv is now closed, so a refetch triggered by
    # this event re-materializes filesystem state from the NEW agent's spec.
    # This is what flips the web Files tab when the switch crosses an
    # os_env boundary (none→some shows it, some→none hides it) — the
    # session.agent_changed event fires before the reset and so cannot
    # carry a trustworthy availability signal.
    _publish_changed_files_invalidated(session_id)

