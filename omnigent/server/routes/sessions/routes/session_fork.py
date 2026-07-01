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

def register_session_fork(
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
        # ── POST /sessions/{source_id}/fork ─────────────────────────

        @router.post(
            "/sessions/{source_id}/fork",
            status_code=201,
            # response_model=None: handler returns SessionResponse
            # but we suppress the OpenAPI schema injection to match
            # the convention of sibling routes.
            response_model=None,
        )
        async def fork_session(
            request: Request,
            source_id: str,
            body: SessionForkRequest,
        ) -> SessionResponse:
            """
            Fork an existing session into a new session.

            Deep-copies the source session's conversation items and
            clones the agent into a new session. When ``body.agent_id``
            is set, the fork binds that built-in agent instead of the
            source's — switching harness (e.g. Claude-SDK → Claude Code,
            or Claude → Codex). The source's model settings carry over
            only within the same provider family; a same-family native
            target also carries conversation history (the runner rebuilds
            its transcript). The REPL/CLI binds the fork to its runner via
            ``PATCH /v1/sessions/{id}`` after creation.

            When ``body.up_to_response_id`` is set, only history up to and
            including that response is copied into the fork (a "fork from
            this response"); a native target then rebuilds its transcript
            from the truncated items instead of resuming the source's full
            native transcript.

            :param request: The incoming FastAPI request (for auth).
            :param source_id: Session/conversation identifier of the
                source session to fork, e.g. ``"conv_abc123"``.
            :param body: The validated :class:`SessionForkRequest`.
            :returns: A :class:`SessionResponse` describing the newly
                created fork (status ``"idle"``).
            :raises OmnigentError: 404 if *source_id* does not exist
                or ``body.agent_id`` is not a bindable built-in agent;
                403 if the caller lacks read access; 400 if the source
                is a sub-agent session, has no agent binding, or
                ``body.up_to_response_id`` names no response in the
                source session.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, source_id, LEVEL_READ, permission_store, conversation_store
            )
            source = access.conversation
            if source is None:
                source = await asyncio.to_thread(conversation_store.get_conversation, source_id)
                if source is None:
                    raise OmnigentError(
                        f"Session not found: {source_id!r}",
                        code=ErrorCode.NOT_FOUND,
                    )
            if source.kind == "sub_agent":
                raise OmnigentError(
                    "Cannot fork a sub-agent session — only top-level sessions can be forked.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if source.agent_id is None:
                raise OmnigentError(
                    "Source session has no agent binding — cannot fork.",
                    code=ErrorCode.INVALID_INPUT,
                )

            source_agent = await asyncio.to_thread(agent_store.get, source.agent_id)
            if source_agent is None:
                raise OmnigentError(
                    f"Source agent not found: {source.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )

            # By default the fork clones the source's agent (same harness). When
            # ``body.agent_id`` names a different agent, the fork SWITCHES to it
            # — e.g. fork a Claude-SDK session into Claude Code. Only built-in
            # agents (``session_id IS NULL``) are bindable: a session-scoped
            # agent belongs to one conversation (possibly another user's) and
            # must never be cloned across sessions.
            base_agent = source_agent
            switching_agent = False
            if body.agent_id is not None and body.agent_id != source.agent_id:
                target_agent = await asyncio.to_thread(
                    require_agent_ref,
                    agent_store,
                    body.agent_id,
                    template_only=True,
                    not_found=f"Agent not found or not bindable: {body.agent_id!r}",
                )
                switching_agent = target_agent.id != source.agent_id
                if switching_agent:
                    base_agent = target_agent

            # Clone the chosen agent's bundle into a fresh session-scoped
            # AgentStore definition so the fork can be reconfigured without
            # mutating the original.
            cloned_agent_id = generate_agent_id()
            cloned_agent_name = f"{base_agent.name} (fork {cloned_agent_id[:10]})"

            # A model id is provider-bound, so the source's model_override /
            # reasoning_effort only carry over when the switch stays in the same
            # provider family. A cross-family switch (or an undeterminable
            # family) resets them; same-agent forks always copy.
            copy_model_settings = True
            if switching_agent:
                copy_model_settings = await asyncio.to_thread(
                    _same_provider_family, source_agent, base_agent
                )

            # When the fork binds a NATIVE target, the native CLI won't replay
            # the copied Omnigent transcript on its own — mark the fork so the
            # runner carries history into the native harness. Same-family: clone
            # the source's native transcript when present, else rebuild from the
            # copied Omnigent items. Cross-family: the source's native transcript
            # is the wrong format, so ALWAYS rebuild from the copied Omnigent
            # items (the converters consume Omnigent's normalized item shape, so
            # the source harness doesn't matter). SDK targets replay the
            # transcript as context regardless, so the marker is inert for them.
            carry_history_into_native = await asyncio.to_thread(_agent_is_native, base_agent)
            # The source's native session id is only resumable by a target in the
            # SAME provider family — a Claude target can't clone a Codex rollout.
            # Cross-family, the store must skip the fork-source directive so the
            # runner takes the rebuild path instead of a doomed clone attempt
            # (a failed clone launches fresh, losing history).
            resume_source_native_session = not switching_agent or copy_model_settings

            # On an agent switch, recompute the Web UI presentation labels for
            # the TARGET harness so the clone isn't left in the source's UI mode
            # (e.g. a claude-native source's terminal-first labels would put an
            # SDK clone in terminal mode with a stale interactive terminal).
            # A same-agent fork leaves the copied labels untouched (None).
            presentation_labels = (
                await asyncio.to_thread(_presentation_labels_for_agent, base_agent)
                if switching_agent
                else None
            )

            try:
                new_conv = await asyncio.to_thread(
                    conversation_store.fork_conversation,
                    source_id,
                    title=body.title,
                    agent_id=cloned_agent_id,
                    copy_model_settings=copy_model_settings,
                    carry_history_into_native=carry_history_into_native,
                    resume_source_native_session=resume_source_native_session,
                    presentation_labels=presentation_labels,
                    up_to_response_id=body.up_to_response_id,
                )
                await asyncio.to_thread(
                    agent_store.create,
                    agent_id=cloned_agent_id,
                    name=cloned_agent_name,
                    bundle_location=base_agent.bundle_location,
                    description=base_agent.description,
                    session_id=new_conv.id,
                )
            except LookupError as exc:
                raise OmnigentError(
                    f"Session not found: {source_id!r}",
                    code=ErrorCode.NOT_FOUND,
                ) from exc
            except ValueError as exc:
                # Store raises ValueError when up_to_response_id names no
                # response in the source conversation (stale client state).
                raise OmnigentError(
                    str(exc),
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            except Exception:
                if "new_conv" in locals():
                    await conversation_store.delete_conversation(new_conv.id)
                raise

            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(permission_store.grant, user_id, new_conv.id, LEVEL_OWNER)
            # Push the forked session to this user's other open tabs.
            _announce_session_added(user_id, new_conv.id)

            fork_items = await asyncio.to_thread(
                conversation_store.list_items, new_conv.id, limit=10000
            )
            level = await _get_permission_level(user_id, new_conv.id, permission_store)
            return _build_session_response(
                new_conv,
                fork_items.data,
                "idle",
                permission_level=level,
                last_task_error=None,
                agent_name=base_agent.name,
            )

