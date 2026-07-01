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

def register_session_stream(
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
        # ── GET /sessions/{session_id}/stream ────────────────────────

        # Live-tail plus bounded Last-Event-ID resume. Clients still reconcile via
        # GET /v1/sessions/{id} for snapshot and item-id dedupe (see API.md).
        @router.get(
            "/sessions/{session_id}/stream",
            # response_model=None: returns StreamingResponse, not a model.
            response_model=None,
            # responses=: surface the SSE union to OpenAPI. The
            # ``text/event-stream`` content entry's schema points at the
            # discriminated union so generated clients know what to
            # expect on the wire. ``scripts/dump_openapi.py`` rewrites
            # this in OpenAPI 3.2's ``itemSchema`` form (the OAS 3.2
            # mechanism for typing each item in a sequential stream)
            # before writing ``openapi.json`` to disk.
            responses={
                200: {
                    "description": ("SSE stream of :data:`ServerStreamEvent` frames for the session."),
                    "content": {
                        "text/event-stream": {
                            "schema": {"$ref": "#/components/schemas/ServerStreamEvent"},
                        },
                    },
                },
            },
        )
        async def stream_session(
            request: Request,
            session_id: str,
            idle: bool = False,
        ) -> StreamingResponse:
            """
            Subscribe to the session's live SSE event stream.

            Does not replay durable history; bounded Last-Event-ID resume covers
            recent live events, and clients reconcile older gaps via the snapshot
            endpoint. The generator handles disconnects via a
            ``try/finally`` that emits the ``[DONE]`` sentinel in all
            exit paths — see :func:`_stream_live_events`.

            Holding this stream open registers the caller as a session
            *viewer* (presence): co-viewers' streams receive
            ``session.presence`` events on join/leave/idle edges, and
            this stream's snapshot-on-connect includes the current
            viewer list. Presence is scoped to the session tree's root
            conversation, so viewers of different agents/sub-agents in
            one session see each other. See
            ``omnigent/server/presence.py``.

            :param request: The FastAPI request, used to detect
                disconnect.
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param idle: Presence idle flag computed by the web client
                at connect time (tab backgrounded ≥ its debounce). An
                idle *flip* mid-view arrives as a reconnect carrying the
                new value — there is no separate update endpoint.
            :returns: An SSE :class:`StreamingResponse`.
            :raises OmnigentError: 404 if no session exists.
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
            runner_client = await _get_runner_client(
                session_id,
                runner_router,
            )
            if (
                runner_client is None
                and conv.runner_id is not None
                and conv.host_id is not None
                and await _heal_session_runner(session_id, request)
            ):
                conv = (
                    await asyncio.to_thread(conversation_store.get_conversation, session_id)
                    or conv
                )
                runner_client = await _get_runner_client(session_id, runner_router)
            try:
                await _ensure_runner_relay_ready(
                    session_id,
                    conv.runner_id,
                    runner_client,
                    conversation_store,
                )
            except OmnigentError as exc:
                # A dead runner on the eager session-open stream must self-heal, not
                # 503-storm (BDP-2579 rung 1/3). Relaunch/fail-over the runner, then
                # rebind the relay once; if it still can't come up, serve the local
                # session stream degraded — the heal set the reconnecting state
                # (terminal_pending), so the UI shows "reconnecting", never a loop.
                if not _is_runner_unavailable_error(exc):
                    raise
                if await _heal_session_runner(session_id, request):
                    conv = (
                        await asyncio.to_thread(
                            conversation_store.get_conversation, session_id
                        )
                        or conv
                    )
                    runner_client = await _get_runner_client(session_id, runner_router)
                    with contextlib.suppress(OmnigentError):
                        await _ensure_runner_relay_ready(
                            session_id,
                            conv.runner_id,
                            runner_client,
                            conversation_store,
                        )

            async def _resource_snapshot() -> list[dict[str, Any]]:
                """Gather current resource state to emit as snapshot-on-connect.

                Best-effort: every runner-touching gather is time-boxed and
                guarded so a slow/unavailable runner never blocks the live
                tail. Terminals arrive as ``session.resource.created`` (the
                same shape the web's live handler already consumes); child
                sessions as ``session.child_session.updated``; changed files
                as a single invalidate that triggers a client refetch.

                The in-flight assistant-text replay is NOT read here: it is
                dedup-sensitive and must be captured synchronously at slot
                registration via ``subscribe``'s ``pre_ready_snapshot`` hook,
                before ``ready_event`` suspends. The resource
                gathers below need awaits and are not dedup-sensitive, so they
                stay in this async hook.
                """
                events: list[dict[str, Any]] = []
                try:
                    page = await asyncio.to_thread(
                        conversation_store.list_conversations,
                        limit=100,
                        kind="sub_agent",
                        parent_conversation_id=session_id,
                        order="desc",
                        sort_by="created_at",
                    )
                    summaries = await _child_session_summaries_from_conversations(
                        page.data,
                        session_id,
                        conversation_store,
                    )
                    for summary in summaries:
                        events.append(
                            {
                                "type": "session.child_session.updated",
                                "conversation_id": session_id,
                                "child_session_id": summary.id,
                                "child": summary.model_dump(mode="json"),
                            }
                        )
                except Exception:  # noqa: BLE001 -- best-effort snapshot; never block live tail
                    _logger.debug("snapshot: child sessions failed for %s", session_id, exc_info=True)
                try:
                    resp = await asyncio.wait_for(
                        # order=asc: the web cache appends each replayed
                        # ``created`` event, so the replay must arrive in
                        # creation order or the session's own terminal (always
                        # created first) lands behind later agent-launched
                        # ones. limit=1000 (the runner endpoint max) keeps the
                        # oldest-first window from dropping the newest
                        # terminals past the default page of 20.
                        runner_client.get(
                            f"/v1/sessions/{session_id}/resources/terminals",
                            params={"order": "asc", "limit": "1000"},
                        ),
                        timeout=_SNAPSHOT_RUNNER_TIMEOUT_S,
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("data", []):
                            events.append({"type": "session.resource.created", "resource": item})
                except Exception:  # noqa: BLE001 -- best-effort snapshot; never block live tail
                    _logger.debug("snapshot: terminals failed for %s", session_id, exc_info=True)
                # Tell the client to (re)fetch the changed-files list rather
                # than fetching it here (avoids a second runner round-trip).
                events.append(
                    {
                        "type": "session.changed_files.invalidated",
                        "session_id": session_id,
                        "environment_id": "default",
                    }
                )
                # Current viewer list (full state, includes this stream's own
                # registration) so a joiner never waits for the next presence
                # edge to learn who's here. Scoped to the session tree's root
                # so a sub-agent page sees viewers of every agent in the tree.
                events.append(presence.snapshot(conv.root_conversation_id, session_id))
                return events

            return StreamingResponse(
                _stream_live_events(
                    request,
                    session_id,
                    _resource_snapshot,
                    # Presence tracks distinct human actors only — the reserved
                    # single-user "local" sentinel maps to None (no tracking),
                    # same as message attribution.
                    viewer_user_id=_attribution_user(user_id),
                    viewer_idle=idle,
                    # Scope presence to the tree's root: sub-agent pages open
                    # the CHILD conversation's stream, and per-conversation
                    # scoping would hide co-viewers on other agents.
                    presence_root_id=conv.root_conversation_id,
                    # Last-Event-ID resume (BDP-2391): a reconnecting EventSource
                    # resends the last id it saw; replay the buffered suffix.
                    last_event_id=_parse_last_event_id(request),
                    # Keep this conversation's runner warm while the user holds the
                    # stream open (BDP-2601). Only for host-bound runner sessions;
                    # in-process / unbound sessions have no runner to warm.
                    keepalive_runner_router=(
                        runner_router
                        if conv.runner_id is not None and conv.host_id is not None
                        else None
                    ),
                ),
                media_type="text/event-stream",
                headers={
                    # Keep intermediaries from buffering the SSE stream:
                    # ``X-Accel-Buffering: no`` disables nginx-style response
                    # buffering so heartbeats and deltas reach the client as
                    # they're written (a buffered proxy can delay the 15s
                    # heartbeat past a client/idle timeout), and ``no-cache``
                    # keeps the long-lived response out of any shared cache.
                    # NOTE: this does NOT defeat the Databricks Apps ingress'
                    # hard ~5-min HTTP/2 stream-duration cap — that drop is
                    # handled by the client's transparent reconnect.
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

