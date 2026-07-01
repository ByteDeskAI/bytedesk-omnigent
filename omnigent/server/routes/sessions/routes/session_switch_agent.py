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

def register_session_switch_agent(
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
        # ── POST /sessions/{session_id}/switch-agent ─────────────────

        @router.post(
            "/sessions/{session_id}/switch-agent",
            # response_model=None: handler returns SessionResponse but we
            # suppress the OpenAPI schema injection to match sibling routes.
            response_model=None,
        )
        async def switch_session_agent(
            request: Request,
            session_id: str,
            body: SessionSwitchAgentRequest,
            background_tasks: BackgroundTasks,
        ) -> SessionResponse:
            """
            Switch an existing session in place to a different agent/harness.

            Unlike fork, this keeps the SAME session — transcript, comments,
            files, host, and workspace are untouched; only the agent/harness
            changes. The current session-scoped agent is replaced by a clone
            of the target built-in, model settings carry over only within the
            same provider family (a model id is provider-bound), the native
            runtime session id is cleared, and the harness-presentation labels
            are recomputed for the target. The next turn cold-starts the new
            harness (rebuilding the native transcript from this session's own
            items for a same-family native target). Only built-in agents are
            bindable, and only while the session is idle.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier to switch,
                e.g. ``"conv_abc123"``.
            :param body: The validated :class:`SessionSwitchAgentRequest`.
            :returns: A :class:`SessionResponse` describing the session after
                the switch (status ``"idle"``).
            :raises OmnigentError: 404 if the session or target agent does
                not exist or the target is not a bindable built-in; 403 if the
                caller lacks edit access; 400 if the session is a sub-agent,
                has no agent binding, or the target bundle can't be loaded;
                409 if a turn is currently running.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            session = access.conversation
            if session is None:
                session = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if session is None:
                    raise OmnigentError(
                        f"Session not found: {session_id!r}",
                        code=ErrorCode.NOT_FOUND,
                    )
            if session.kind == "sub_agent":
                raise OmnigentError(
                    "Cannot switch the agent of a sub-agent session — only top-level "
                    "sessions can switch agent.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if session.agent_id is None:
                raise OmnigentError(
                    "Session has no agent binding — cannot switch agent.",
                    code=ErrorCode.INVALID_INPUT,
                )

            # Switching mid-turn would tear the running harness subprocess out
            # from under an active stream. Reject; the caller retries when idle.
            if _session_status_from_cache(session_id) == "running":
                raise OmnigentError(
                    "Session is busy — wait for the current turn to finish before switching agent.",
                    code=ErrorCode.CONFLICT,
                )

            current_agent = await asyncio.to_thread(agent_store.get, session.agent_id)
            if current_agent is None:
                raise OmnigentError(
                    f"Current agent not found: {session.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )

            # Only built-in agents (``session_id IS NULL``) are bindable: a
            # session-scoped agent belongs to one conversation (possibly another
            # user's) and must never be cloned across sessions.
            target_agent = await asyncio.to_thread(
                require_agent_ref,
                agent_store,
                body.agent_id,
                template_only=True,
                not_found=f"Agent not found or not bindable: {body.agent_id!r}",
            )

            # Reject a no-op switch to the built-in the session is already running:
            # its session-scoped clone shares the built-in's ``bundle_location``, so
            # switching would delete + re-clone the same agent and tear the terminal
            # down for nothing. The contract is that the target differs from the
            # current agent; the picker already hides the current one, so this only
            # guards a direct API call.
            if target_agent.bundle_location == current_agent.bundle_location:
                raise OmnigentError(
                    "Session is already running this agent — pick a different one.",
                    code=ErrorCode.INVALID_INPUT,
                )

            # Load the target bundle BEFORE committing so an unloadable spec fails
            # the request with zero mutation — the irreversible part of the switch
            # (deleting the old agent) must not run for a target that can't start.
            try:
                await asyncio.to_thread(
                    get_agent_cache().load, target_agent.id, target_agent.bundle_location
                )
            except Exception as exc:
                # Surface any bundle-load failure as a 400 before mutating state.
                raise OmnigentError(
                    f"Target agent bundle could not be loaded: {body.agent_id!r}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

            # A model id is provider-bound, so model_override / reasoning_effort
            # carry over only within the same provider family. A native target
            # carries history regardless of family: the switch clears
            # external_session_id and drops the fork-source directive, so the
            # runner rebuilds the native transcript from this session's own
            # Omnigent items (a format-agnostic conversion). SDK targets replay
            # the AP transcript as context regardless.
            copy_model_settings = await asyncio.to_thread(
                _same_provider_family, current_agent, target_agent
            )
            carry_history_into_native = await asyncio.to_thread(_agent_is_native, target_agent)
            presentation_labels = await asyncio.to_thread(_presentation_labels_for_agent, target_agent)

            # Resolve the built-in the session is leaving so the UI can offer a
            # one-click "Switch back". The current agent is a session-scoped clone
            # whose bundle_location was copied verbatim from its source built-in,
            # so match on that. Page through the full template-agent list (not a
            # single bounded scan) so the match isn't missed when there are many
            # built-ins. Best-effort: None when no built-in matches (e.g. its
            # source built-in was removed) → no switch-back offered.
            previous_builtin_id: str | None = None
            _after: str | None = None
            while True:
                _page = await asyncio.to_thread(agent_store.list, 100, _after)
                previous_builtin_id = next(
                    (a.id for a in _page.data if a.bundle_location == current_agent.bundle_location),
                    None,
                )
                if previous_builtin_id is not None or not _page.has_more or not _page.data:
                    break
                _after = _page.last_id

            cloned_agent_id = generate_agent_id()
            cloned_agent_name = f"{target_agent.name} (switch {cloned_agent_id[:10]})"
            new_agent_created = False
            try:
                await asyncio.to_thread(
                    agent_store.create,
                    agent_id=cloned_agent_id,
                    name=cloned_agent_name,
                    bundle_location=target_agent.bundle_location,
                    description=target_agent.description,
                    session_id=session_id,
                    replace_session=True,
                )
                new_agent_created = True
                updated = await asyncio.to_thread(
                    conversation_store.switch_conversation_agent,
                    session_id,
                    new_agent_id=cloned_agent_id,
                    new_agent_name=cloned_agent_name,
                    new_agent_bundle_location=target_agent.bundle_location,
                    new_agent_description=target_agent.description,
                    copy_model_settings=copy_model_settings,
                    carry_history_into_native=carry_history_into_native,
                    presentation_labels=presentation_labels,
                    previous_builtin_id=previous_builtin_id,
                )
            except LookupError as exc:
                if new_agent_created:
                    await asyncio.to_thread(agent_store.delete, cloned_agent_id)
                    if current_agent.session_id == session_id:
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(
                                agent_store.create,
                                agent_id=current_agent.id,
                                name=current_agent.name,
                                bundle_location=current_agent.bundle_location,
                                description=current_agent.description,
                                session_id=session_id,
                                replace_session=True,
                            )
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                ) from exc
            except Exception:
                if new_agent_created:
                    await asyncio.to_thread(agent_store.delete, cloned_agent_id)
                    if current_agent.session_id == session_id:
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(
                                agent_store.create,
                                agent_id=current_agent.id,
                                name=current_agent.name,
                                bundle_location=current_agent.bundle_location,
                                description=current_agent.description,
                                session_id=session_id,
                                replace_session=True,
                            )
                raise

            # Tell every connected client the binding changed so they re-derive
            # session state (presentation labels, bound agent) from a fresh
            # snapshot. Without this, a client that bound before the switch keeps
            # treating the session as the OLD harness — e.g. its status handler
            # clears the optimistic first-message bubble that a native target
            # only reconciles later via session.input.consumed.
            switch_event = SessionAgentChangedEvent(
                type="session.agent_changed",
                conversation_id=session_id,
                agent_id=cloned_agent_id,
                # Clean target name, not the clone row's "<name> (switch ag_…)":
                # the suffix only disambiguates agent rows; clients render
                # agent_name verbatim (same choice as the session snapshot).
                agent_name=target_agent.name,
            )
            session_stream.publish(session_id, switch_event.model_dump())

            # Reset the OLD harness's runner-side resources (async, after the
            # response): close the cached primary OSEnv so the new agent's
            # os_env/sandbox governs the web filesystem/shell endpoints, and tear
            # down the native terminal so it can't shadow the switch-back transcript
            # rebuild. Safe because the switch only runs while the session is idle
            # (doing it mid-turn would wedge the turn); the next access
            # re-materializes from the new agent's spec, preserving the workspace /
            # worktree (cwd comes from the runner workspace).
            background_tasks.add_task(_reset_runner_resources_after_switch, session_id)

            items = await asyncio.to_thread(conversation_store.list_items, session_id, limit=10000)
            level = await _get_permission_level(user_id, session_id, permission_store)
            return _build_session_response(
                updated,
                items.data,
                "idle",
                permission_level=level,
                last_task_error=None,
                agent_name=target_agent.name,
            )

