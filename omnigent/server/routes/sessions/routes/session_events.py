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
from omnigent.communications import (
    ChatActor,
    ChatActorKind,
    ChatApplicationService,
    ChildSessionDelegationService,
    DelegateToAgentCommand,
    PostSessionEventCommand,
)
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

def register_session_events(
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
        chat_application_service = ChatApplicationService(
            allowed_event_types=_ALLOWED_EVENT_TYPES,
            payload_validation_exempt_event_types=frozenset(
                {
                    _INTERRUPT_TYPE,
                    _APPROVAL_TYPE,
                    _MCP_ELICITATION_TYPE,
                    _COMPACT_TYPE,
                    _SLASH_COMMAND_TYPE,
                    _STOP_SESSION_TYPE,
                    _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
                    _EXTERNAL_CONVERSATION_ITEM_TYPE,
                    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
                    _EXTERNAL_SESSION_INTERRUPTED_TYPE,
                    _EXTERNAL_ELICITATION_RESOLVED_TYPE,
                    _EXTERNAL_SESSION_STATUS_TYPE,
                    _EXTERNAL_SESSION_USAGE_TYPE,
                    _EXTERNAL_COMPACTION_STATUS_TYPE,
                    _EXTERNAL_MODEL_CHANGE_TYPE,
                    _EXTERNAL_SESSION_TODOS_TYPE,
                    _EXTERNAL_SUBAGENT_START_TYPE,
                    _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
                }
            ),
            item_payload_validator=parse_item_data,
            tool_spec_validator=parse_client_side_tool_specs,
        )
        # ── POST /sessions/{session_id}/events ───────────────────────

        @router.post(
            "/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            status_code=202,
            # response_model=None: the body is a small acknowledgement
            # dict, not a domain model.
            response_model=None,
        )
        async def resolve_elicitation(
            request: Request,
            session_id: str,
            elicitation_id: str,
            body: ElicitationResult,
        ) -> dict[str, bool]:
            """
            Resolve an outstanding elicitation by its URL (URL-based
            elicitation).

            The dedicated, RESTful counterpart to delivering a verdict
            via the ``type == "approval"`` event on
            ``POST /v1/sessions/{id}/events``. An elicitation request
            published in ``mode == "url"`` carries this endpoint's path
            as its ``params.url``; the client hits it directly with the
            MCP :class:`ElicitationResult` body instead of POSTing a
            generic approval event. The verdict routes through the
            shared :func:`_resolve_elicitation`, so resolution semantics
            are identical to the event path.

            The ``elicitation_id`` is taken from the URL rather than the
            body, so the unguessable id (``secrets.token_hex(16)``) is
            the capability scoping the resolution — combined with the
            session-owner ``LEVEL_EDIT`` gate below and the server-side
            ownership check inside :func:`_resolve_elicitation`.

            :param request: The inbound request, used for identity
                extraction.
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param elicitation_id: Correlation id of the elicitation to
                resolve, e.g. ``"elicit_abc123"``. Taken from the URL
                path, not the body.
            :param body: The MCP-shaped verdict — ``action``
                (``"accept"`` / ``"decline"`` / ``"cancel"``) plus
                optional form ``content``.
            :returns: ``{"queued": False}`` — resolution is synchronous
                and persists no conversation item.
            :raises OmnigentError: 404 if no session exists.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
            _resolve_data = {"elicitation_id": elicitation_id, **body.model_dump(exclude_none=True)}
            await _resolve_elicitation(session_id, _resolve_data, runner_router, conversation_store)
            # Apply any policy writes deferred by the relay tool-call ASK gate
            # (e.g. a cost-budget checkpoint) now that the verdict is in.
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, _resolve_data
            )
            return {"queued": False}

        @router.get(
            "/sessions/{session_id}/elicitations/{elicitation_id}",
            response_model=None,
        )
        async def get_elicitation(
            request: Request,
            session_id: str,
            elicitation_id: str,
        ) -> dict[str, Any]:
            """
            Return the state of a pending elicitation as JSON.

            Used by the frontend's standalone approval page
            (``/approve/:sessionId/:elicitationId``) to fetch the
            elicitation prompt and render approve/reject controls.
            The payload is read from the in-memory
            :mod:`omnigent.runtime.pending_elicitations` index — no
            database persistence required.

            :param request: The inbound request, used for identity
                extraction.
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param elicitation_id: Correlation id of the elicitation,
                e.g. ``"elicit_abc123"``.
            :returns: JSON with ``status`` (``"pending"`` or
                ``"resolved"``), and when pending: ``message``,
                ``phase``, ``policy_name``, ``content_preview``.
            :raises OmnigentError: 404 if the session does not exist.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            if access.conversation is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )

            found = pending_elicitations.lookup(elicitation_id)
            if found is None or found[0] != session_id:
                return {"status": "resolved"}

            _conv_id, event = found
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            return {
                "status": "pending",
                "message": params.get("message", "Approval required"),
                "phase": params.get("phase", ""),
                "policy_name": params.get("policy_name", ""),
                "content_preview": params.get("content_preview", ""),
            }

        @router.post(
            "/sessions/{session_id}/events",
            status_code=202,
            # response_model=None: the body is a small acknowledgement
            # dict, not a domain model.
            response_model=None,
        )
        async def post_event(
            request: Request,
            session_id: str,
            body: SessionEventInput,
        ) -> dict[str, bool | str]:
            """
            Submit a session event (input message, tool output,
            approval, or interrupt).

            Dispatches on ``body.type``:

            - ``"interrupt"`` cancels any active task and publishes a
              ``session.interrupted`` event. Bypasses item persistence.
            - ``"approval"`` resolves an outstanding elicitation
              in-band (see :func:`_dispatch_approval`).
            - ``"external_assistant_message"`` appends and streams an
              assistant message observed outside the Omnigent task runtime,
              without starting or steering a task.
            - ``"external_conversation_item"`` appends and streams a
              completed item observed outside the Omnigent task runtime,
              without starting or steering a task.
            - ``"external_output_text_delta"`` publishes a transient
              ``response.output_text.delta`` event observed outside the
              Omnigent task runtime, without persisting an item or starting /
              steering a task.
            - ``"external_session_interrupted"`` publishes a
              ``session.interrupted`` event observed outside the Omnigent task
              runtime, without persisting an item or starting / steering a
              task.
            - ``"external_elicitation_resolved"`` marks a native
              harness-originated elicitation as resolved elsewhere so
              subscribed clients clear the pending approval card.
            - ``"external_session_status"`` publishes a terminal-observed
              ``session.status`` edge without persisting an item or
              starting/steering a task.
            - ``"external_model_change"`` persists a terminal-observed
              model switch to ``model_override`` and publishes a
              ``session.model`` SSE event so the web picker reflects it.
            - ``"stop_session"`` terminates the live session without
              deleting the conversation (owner-only). Forwarded
              harness-agnostically to the runner, which hard-kills the
              external process for harnesses that have one (claude-native
              kills its tmux pane) and 204s otherwise. Stop is non-sticky:
              it writes no persistent marker, so the next message
              auto-relaunches the session on its (still-online) host via
              the normal message-dispatch relaunch path.
            - ``"message"`` on an ``omnigent claude`` terminal session
              is forwarded to the bound runner for tmux injection only;
              the accepted prompt is persisted later when Claude records
              it in the terminal transcript.
            - Any other (item-typed) event is persisted into
              ``conversation_items`` via the legacy create-or-steer path
              (legacy persist path): if an active
              task is present, the item is delivered into its inbox;
              otherwise a new task is created and started. In both
              cases ``session.input.consumed`` fires with the persisted
              item's id.

            :param session_id: Session/conversation identifier.
            :param body: The validated :class:`SessionEventInput`.
            :returns: ``{"queued": True, "item_id": "..."}`` for
                item-typed events, where ``item_id`` is the persisted
                conversation item id also emitted by
                ``session.input.consumed``; ``{"queued": False}`` for
                control and internal transient events.
            :raises OmnigentError: 404 if no session exists.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
            # BDP-2434: refresh the per-session OBO subject_token from this inbound
            # event (Office re-sends ``X-Bytedesk-Subject-Token`` on each post), so a
            # later ``tools/call`` mint presents a fresh on-behalf-of bearer. No-op
            # when the header is absent (degrade-to-default).
            stash_subject_token_from_headers(request.app.state, session_id, request.headers)
            chat_application_service.admit_post_event(
                PostSessionEventCommand(
                    session_id=session_id,
                    actor=ChatActor(kind=ChatActorKind.USER, user_id=user_id),
                    event_type=body.type,
                    payload=body.data,
                    tool_specs=tuple(body.tools or ()),
                )
            )
            # ── Policy evaluation (path-agnostic) ────────────────
            # Evaluate policies BEFORE persistence/runner forwarding so
            # enforcement fires on both paths. On DENY, persist the
            # event (possibly with modified body) through whichever
            # path is active, then return the deny verdict. On ALLOW,
            # fall through to the normal persist/forward path.
            _policy_body = body  # may be replaced by OUTPUT deny
            _actor = _build_actor(user_id)
            # A closed sub-agent session (sys_session_close) rejects new user
            # input — the orchestrator must spawn a fresh session to continue.
            if (
                body.type == "message"
                and body.data.get("role") == "user"
                and is_session_closed(conv.labels, conv.title)
            ):
                raise OmnigentError(
                    "Session is closed. Start a new sub-agent session to continue.",
                    code=ErrorCode.CONFLICT,
                )
            if (
                body.type == "message"
                and body.data.get("role") == "user"
                and conv.agent_id is not None
            ):
                try:
                    _input_verdict = await _evaluate_input_policy(
                        request,
                        session_id,
                        conv,
                        body,
                        conversation_store,
                        agent_store,
                        runner_router,
                        actor=_actor,
                    )
                except Exception as _policy_exc:  # noqa: BLE001 — fail-safe for misconfigured policies
                    # Policy evaluation crashed (e.g. factory misconfigured).
                    # Log and treat as DENY so the session doesn't hang on
                    # "working" forever. The full cause is logged for admins;
                    # the denial reason returned to (and streamed at) the client
                    # stays generic so the raw exception text isn't exposed.
                    _logger.warning(
                        "Input policy evaluation failed for %s: %s",
                        session_id,
                        _policy_exc,
                        exc_info=True,
                    )
                    _input_verdict = {
                        "verdict": "deny",
                        "reason": "Denied by policy (policy evaluation error).",
                    }
                if _input_verdict is not None:
                    # DENY or ASK — don't forward to runner. Publish a
                    # deny sentinel on the session stream so the
                    # client/REPL sees feedback.
                    reason = _input_verdict.get("reason", "Denied by policy")
                    _publish_status(session_id, "running")
                    _publish_policy_deny(session_id, reason)
                    _publish_status(session_id, "idle")
                    # Return the same shape the client expects from POST
                    # /events so postEvent doesn't throw on an unexpected
                    # response body. queued=False signals the event was
                    # handled synchronously (denied, not queued for a turn).
                    return {"queued": False, "denied": True, "reason": reason}
            elif body.type == _SLASH_COMMAND_TYPE and conv.agent_id is not None:
                _input_verdict = await _evaluate_input_policy(
                    request,
                    session_id,
                    conv,
                    _build_skill_slash_command_policy_body(body),
                    conversation_store,
                    agent_store,
                    runner_router,
                )
                if _input_verdict is not None:
                    reason = _input_verdict.get("reason", "Denied by policy")
                    _publish_status(session_id, "running")
                    _publish_policy_deny(session_id, reason)
                    _publish_status(session_id, "idle")
                    return {"queued": False, "denied": True, "reason": reason}
            elif (
                body.type == "message"
                and body.data.get("role") == "assistant"
                and conv.agent_id is not None
            ):
                _output_verdict = await _evaluate_output_policy(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    agent_store,
                    runner_router,
                    actor=_actor,
                )
                if _output_verdict is not None:
                    if _output_verdict.get("_denied_body") is not None:
                        _policy_body = _output_verdict["_denied_body"]
                        body = _policy_body
                    # For OUTPUT DENY, fall through to persist the
                    # denied body (with sentinel text). The verdict
                    # is returned after persistence below.
                    if _output_verdict["verdict"] == "deny":
                        pass  # fall through with modified body
                    else:
                        return _output_verdict
            elif body.type == "function_call" and body.data.get("evaluate_policy"):
                _tool_verdict = await _evaluate_tool_call_policy(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    agent_store,
                    runner_router,
                    actor=_actor,
                )
                if _tool_verdict is not None:
                    return _tool_verdict
                # ALLOW — return explicit verdict so the request does
                # not fall through to the persist-and-forward path.
                # Policy evaluation requests are queries, not items to
                # persist or relay to the harness (which rejects
                # ``function_call`` as an unknown inbound event type).
                return {"verdict": "allow"}

            if body.type == _INTERRUPT_TYPE:
                _publish_interrupted(session_id)
                # Fence the cancelled turn (see _interrupt_fenced_sessions).
                _interrupt_fenced_sessions.add(session_id)
                runner_client = await _get_runner_client(
                    session_id,
                    runner_router,
                )
                interrupt_delivered = False
                if runner_client is not None:
                    try:
                        interrupt_resp = await runner_client.post(
                            f"/v1/sessions/{session_id}/events",
                            json={"type": "interrupt"},
                            timeout=5.0,
                        )
                        interrupt_delivered = interrupt_resp.status_code < 400
                    except (httpx.HTTPError, ConnectionError):
                        # Runner transports may raise bare ConnectionError.
                        _logger.exception(
                            "Interrupt forward failed for %r",
                            session_id,
                        )
                if not interrupt_delivered:
                    # The turn keeps running and nothing else lifts the fence —
                    # remove it so the turn's remaining output isn't dropped.
                    _interrupt_fenced_sessions.discard(session_id)
                return {"queued": False}
            if body.type == _STOP_SESSION_TYPE:
                # Terminating the whole session (not just the current turn)
                # is a lifecycle action; require owner access on top of the
                # LEVEL_EDIT gate above so a shared editor can't kill the
                # owner's session.
                await _require_access(
                    user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
                )
                # Fence the cancelled turn, same as interrupt.
                _interrupt_fenced_sessions.add(session_id)
                # Harness-agnostic forward: the runner kills the external
                # process for harnesses that have one (claude-native
                # hard-kills its tmux pane) and 204s otherwise. Unlike the
                # best-effort effort/model_change relay, a failed stop means
                # the session is still alive — so this helper RAISES on a
                # non-2xx / unreachable runner (503) rather than swallowing
                # it, letting the web UI show the stop didn't land instead
                # of closing the dialog as if it succeeded.
                try:
                    stop_delivered = await _stop_session_via_runner(session_id, runner_router)
                except Exception:
                    # Stop didn't land: the turn keeps running, so lift the
                    # fence or its remaining output is dropped forever.
                    _interrupt_fenced_sessions.discard(session_id)
                    raise
                if not stop_delivered:
                    # No runner resolved: nothing else lifts the fence (same as interrupt).
                    _interrupt_fenced_sessions.discard(session_id)
                # Host-spawned sessions run on a dedicated runner the host
                # launched for this one session. Killing the pane (above) leaves
                # that runner connected, so GET /health keeps reporting
                # runner_online: true and the web UI never shows the session as
                # disconnected — new messages hang on "working" against a dead
                # pane. Stop the runner too so its tunnel drops and the web UI
                # shows the same "Agent disconnected — click to show reconnect
                # command" banner a CLI-launched session reaches on exit. Read
                # host_id / runner_id from the owner-gated session row so we can
                # only ever stop the runner bound to this session.
                stop_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if stop_conv is not None and stop_conv.host_id and stop_conv.runner_id:
                    await _stop_session_host_runner(
                        session_id,
                        stop_conv.host_id,
                        stop_conv.runner_id,
                        getattr(request.app.state, "host_registry", None),
                    )
                # Stop is non-sticky: no persistent marker is written. The
                # runner tunnel dropping above flips ``runner_online`` to false
                # honestly, and the next message auto-relaunches the session on
                # its (still-online) host via the normal message-dispatch
                # relaunch path below.
                return {"queued": False}
            if body.type == _APPROVAL_TYPE:
                # Deliver the verdict through the shared resolver: it
                # sets any server-side harness Future (owner-checked),
                # clears the sidebar badge, and forwards
                # to the runner for runner-side (policy) elicitations.
                # The dedicated URL endpoint (``.../elicitations/{eid}/
                # resolve``) routes through the same helper.
                await _resolve_elicitation(session_id, body.data, runner_router, conversation_store)
                # Apply any policy writes deferred by the relay tool-call ASK gate
                # (e.g. a cost-budget checkpoint) now that the verdict is in.
                await _apply_pending_policy_ask_writes(
                    session_id, conv, conversation_store, agent_store, body.data
                )
                return {"queued": False}
            if body.type == _MCP_ELICITATION_TYPE:
                # The runner's inline MCP elicitation callback fires when
                # an external MCP server sends ``elicitation/create``
                # during a ``tools/call``. Publish the elicitation as an
                # SSE event (approval card in web UI, y/a/n prompt in
                # REPL) and return the elicitation_id immediately so the
                # runner can park on ``pending_approvals``. The user's
                # verdict arrives later via ``type: "approval"`` →
                # ``_resolve_elicitation`` → ``_forward_approval_to_runner``
                # → runner's ``pending_approvals`` resolves.
                elicit_data = body.data or {}
                elicit_id = f"elicit_{secrets.token_hex(16)}"
                elicit_params = ElicitationRequestParams(
                    mode="form",
                    message=elicit_data.get("message", ""),
                    requestedSchema=elicit_data.get("requestedSchema"),
                )
                event = ElicitationRequestEvent(
                    type="response.elicitation_request",
                    elicitation_id=elicit_id,
                    params=elicit_params,
                )
                _mcp_elicit_payload = event.model_dump()
                session_stream.publish(session_id, _mcp_elicit_payload)
                # Mirror the prompt into ancestor streams so a sub-agent MCP
                # elicitation surfaces in the parent (polly) chat with a
                # ``target_session_id`` pointing back at this child. The
                # verdict still arrives via the generic ``approval`` event,
                # which mirrors the resolved signal back up through
                # ``_resolve_elicitation``.
                await asyncio.to_thread(
                    _publish_elicitation_request_to_ancestors,
                    conversation_store,
                    session_id,
                    _mcp_elicit_payload,
                )
                return {"queued": False, "elicitation_id": elicit_id}
            if body.type == _COMPACT_TYPE:
                # Unified control dispatch (designs/CLAUDE_NATIVE.md
                # "Control events dispatch on the runner"): forward /compact
                # to the bound runner first, regardless of harness. The
                # runner dispatches by harness — claude-native injects
                # /compact into the tmux pane so Claude Code compacts its
                # own context and returns 200; other harnesses 204 no-op.
                # The Omnigent server stays harness-agnostic: it runs its own
                # in-process compaction only when the runner did NOT handle
                # the control (204 / no runner bound). A 4xx/5xx from the
                # runner (e.g. 503 when the claude-native pane isn't
                # attached) is surfaced as an error rather than silently
                # falling through to AP-side compaction, which would be
                # wrong for a terminal-owned session.
                runner_result = await _forward_session_change_to_runner(
                    session_id,
                    runner_router,
                    {"type": _COMPACT_TYPE},
                )
                if runner_result is not None and runner_result.status_code == 200:
                    return {"queued": False}
                if runner_result is not None and runner_result.status_code != 204:
                    raise OmnigentError(
                        f"Compaction failed: runner returned {runner_result.status_code}",
                        code=ErrorCode.INTERNAL_ERROR,
                    )
                await _run_compact_locked(
                    session_id,
                    conv,
                    agent_store,
                    agent_cache,
                )
                return {"queued": False}
            if body.type == "compaction":
                import uuid as _uuid

                item = NewConversationItem(
                    type="compaction",
                    response_id=f"compact_{_uuid.uuid4().hex}",
                    data=parse_item_data("compaction", body.data),
                )
                persisted_items = await asyncio.to_thread(
                    conversation_store.append,
                    session_id,
                    [item],
                )
                # D6 (BDP-2276): rescue the summary into the agent's episodic memory.
                await _rescue_compaction_to_memory(conversation_store, session_id, persisted_items)
                return {"queued": True}
            if body.type == _EXTERNAL_ASSISTANT_MESSAGE_TYPE:
                item_id = await _persist_external_assistant_message(
                    session_id,
                    body,
                    conversation_store,
                )
                return {"queued": False, "item_id": item_id}
            if body.type == _EXTERNAL_CONVERSATION_ITEM_TYPE:
                item_id = await _persist_external_conversation_item(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    created_by=_attribution_user(user_id),
                )
                return {"queued": False, "item_id": item_id}
            if body.type == _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE:
                _publish_external_output_text_delta(session_id, body)
                return {"queued": False}
            if body.type == _EXTERNAL_SESSION_INTERRUPTED_TYPE:
                response_id = body.data.get("response_id")
                if response_id is not None and not isinstance(response_id, str):
                    raise OmnigentError(
                        "external_session_interrupted data.response_id must be a string",
                        code=ErrorCode.INVALID_INPUT,
                    )
                _publish_interrupted(session_id, response_id=response_id)
                return {"queued": False}
            if body.type == _EXTERNAL_ELICITATION_RESOLVED_TYPE:
                elicitation_id = body.data.get("elicitation_id")
                if not isinstance(elicitation_id, str):
                    raise OmnigentError(
                        "external_elicitation_resolved requires string data.elicitation_id.",
                        code=ErrorCode.INVALID_INPUT,
                    )
                _signal_harness_elicitation_resolved_by_id(session_id, elicitation_id)
                return {"queued": False}
            if body.type == _EXTERNAL_SESSION_STATUS_TYPE:
                status = body.data.get("status")
                if status not in _EXTERNAL_SESSION_STATUS_VALUES:
                    raise OmnigentError(
                        f"external_session_status requires data.status in "
                        f"{sorted(_EXTERNAL_SESSION_STATUS_VALUES)}; got {status!r}",
                        code=ErrorCode.INVALID_INPUT,
                    )
                response_id = body.data.get("response_id")
                if response_id is not None and not isinstance(response_id, str):
                    raise OmnigentError(
                        "external_session_status data.response_id must be a string",
                        code=ErrorCode.INVALID_INPUT,
                    )
                if status in {"idle", "failed"}:
                    for flushed in await flush_orphaned_outputs(conversation_store, session_id):
                        _publish_external_conversation_item(session_id, flushed)
                _publish_status(session_id, status, response_id=response_id)
                forward_body = body.model_dump()
                forward_body["data"] = await _enrich_idle_status_with_subagent_output(
                    forward_body["data"], status, session_id, conversation_store
                )
                runner_result = await _forward_session_change_to_runner(
                    session_id,
                    runner_router,
                    forward_body,
                )
                if (
                    conv.kind == "sub_agent"
                    and status in {"idle", "failed"}
                    and not _is_codex_native_subagent(conv)
                ):
                    # Codex-internal children are tracked inside the same
                    # app-server thread tree; they have no runner inbox entry
                    # to forward terminal status to.
                    _require_external_status_forward(
                        session_id,
                        status,
                        runner_result,
                    )
                return {"queued": False}
            if body.type == _EXTERNAL_COMPACTION_STATUS_TYPE:
                # Terminal-observed compaction edge (claude-native forwarder):
                # republish as the standard compaction SSE so the web UI
                # spinner brackets Claude's real terminal compaction. No token
                # count is available here — the context ring is updated
                # separately by external_session_usage — so completed carries
                # total_tokens=None.
                compaction_status = body.data.get("status")
                if compaction_status not in _EXTERNAL_COMPACTION_STATUS_VALUES:
                    raise OmnigentError(
                        f"external_compaction_status requires data.status in "
                        f"{sorted(_EXTERNAL_COMPACTION_STATUS_VALUES)}; got {compaction_status!r}",
                        code=ErrorCode.INVALID_INPUT,
                    )
                if compaction_status == "in_progress":
                    _publish_compaction_in_progress(session_id)
                elif compaction_status == "completed":
                    _publish_compaction_completed(session_id, None)
                else:
                    _publish_compaction_failed(session_id)
                return {"queued": False}
            if body.type == _EXTERNAL_SESSION_USAGE_TYPE:
                # Persist the harness-reported cumulative usage so the
                # tool-call cost gate can read the running
                # ``total_cost_usd`` on the next tool call. (Cost budgets
                # now enforce at ``tool_call`` via the PreToolUse hook, not
                # post-hoc here — a logged output cannot be un-logged.)
                await _persist_external_session_usage(
                    session_id,
                    body,
                    conversation_store,
                )
                return {"queued": False}
            if body.type == _EXTERNAL_MODEL_CHANGE_TYPE:
                await _persist_external_model_change(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                )
                return {"queued": False}
            if body.type == _EXTERNAL_SESSION_TODOS_TYPE:
                _handle_external_session_todos(session_id, body)
                return {"queued": False}
            if body.type == _EXTERNAL_SUBAGENT_START_TYPE:
                child_id = await _persist_external_subagent_start(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                )
                # Returned to the claude-native forwarder so it can address
                # subsequent ``external_conversation_item`` /
                # ``external_session_status`` events to the child id.
                return {"queued": False, "child_session_id": child_id}
            if body.type == _EXTERNAL_CODEX_SUBAGENT_START_TYPE:
                child_id = await _persist_external_codex_subagent_start(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                )
                return {"queued": False, "child_session_id": child_id}
            blueprint_response = await _try_handle_blueprint_session_event(
                request=request,
                session_id=session_id,
                conv=conv,
                body=body,
                user_id=user_id,
            )
            if blueprint_response is not None:
                return blueprint_response
            if body.type == "function_call_output":
                # A client-side tool's result tunneling back to a parked turn.
                # The harness scaffold resolves the parked tool Future on a
                # ``tool_result`` event (ToolResultEvent {call_id, output}), so
                # translate the session-API ``function_call_output`` into that
                # wire shape and forward to the bound runner, which relays it
                # verbatim to the parked harness. Mirrors the runner's own
                # dispatch_tool_locally tool_result post; the output here came
                # from the caller (a client-side tool) instead of a local
                # dispatch. ``parse_item_data`` above already validated the
                # payload against ``FunctionCallOutputData`` (call_id: str,
                # output: str), so both fields are present strings. Stale
                # call_ids no-op at the scaffold; the harness re-emits the
                # completed function_call + output on resume, so history is
                # written through the normal stream path (no separate persist).
                runner_client = await _get_runner_client(session_id, runner_router)
                if runner_client is None:
                    raise OmnigentError(
                        "No runner bound to this session; cannot deliver the tool result.",
                        code=ErrorCode.RUNNER_UNAVAILABLE,
                    )
                try:
                    await runner_client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={
                            "type": "tool_result",
                            "call_id": body.data["call_id"],
                            "output": body.data["output"],
                        },
                        timeout=10.0,
                    )
                except httpx.HTTPError as exc:
                    # Fail loud (503), not best-effort: unlike the advisory
                    # interrupt-forward, a dropped tool_result leaves the parked
                    # turn hanging until it times out. Surfacing the failure lets
                    # the caller retry the delivery (the scaffold no-ops if a
                    # retry double-delivers a now-stale call_id).
                    raise OmnigentError(
                        "Failed to deliver the tool result to the session runner.",
                        code=ErrorCode.RUNNER_UNAVAILABLE,
                    ) from exc
                return {"queued": True, "item_id": body.data["call_id"]}
            # Item event (message, function_call_output, etc.).
            runner_client = await _get_runner_client(session_id, runner_router)
            # A token-bound managed runner can still resolve after its sandbox host
            # has died. Treat that route as stale so the dead-sandbox relaunch path
            # below gets a chance to provision a new generation.
            if runner_client is not None and conv.host_id is not None:
                host_store_for_bound_runner = getattr(request.app.state, "host_store", None)
                if host_store_for_bound_runner is not None:
                    bound_host = await asyncio.to_thread(
                        host_store_for_bound_runner.get_host,
                        conv.host_id,
                    )
                    bound_host_online = await asyncio.to_thread(
                        host_store_for_bound_runner.is_online,
                        conv.host_id,
                    )
                    if (
                        bound_host is not None
                        and bound_host.sandbox_provider is not None
                        and not bound_host_online
                    ):
                        runner_client = None
            # Managed-launch rendezvous: a ``host_type="managed"`` create
            # returns before the sandbox exists, so the first message (the
            # Web UI auto-sends the composer prompt right after navigate)
            # can land while the background provision is still running.
            # Instead of failing with "no runner bound", wait for the
            # launch to settle: success leaves the session host-bound with
            # its runner tunnel already up (the background task awaits
            # it), failure surfaces the recorded reason.
            if runner_client is None and conv.host_id is None:
                _managed_tracker = getattr(request.app.state, "managed_launches", None)
                _managed_launch = (
                    _managed_tracker.get(session_id) if _managed_tracker is not None else None
                )
                if _managed_launch is not None:
                    await _await_settled_managed_launch(_managed_launch)
                    # The launch bound host_id / workspace / runner_id to
                    # the row after this handler's fetch — re-read so the
                    # resolution below sees the bound runner.
                    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                    if conv is None:
                        raise OmnigentError(
                            "Session not found",
                            code=ErrorCode.NOT_FOUND,
                        )
                    runner_client = await _get_runner_client(session_id, runner_router)
            # Whether the runner was initially unavailable but became routable
            # below. In that case the session-init handshake may still be
            # racing the first message, even if we reused the original binding
            # instead of launching a replacement.
            _runner_needs_session_init = False
            if runner_client is None and conv.host_id is not None:
                _runner_control_registry = getattr(request.app.state, "runner_control_registry", None)
                # A just-created host session already has a runner_id before
                # the runner's tunnel is registered. The Web UI can post the
                # first message during that gap; wait briefly for the pinned
                # runner before treating it as dead and replacing it.
                if conv.runner_id is not None and _HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0:
                    _logger.info(
                        "Waiting up to %.1fs for host-bound runner %s to register "
                        "for session %s before relaunch",
                        _HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        conv.runner_id,
                        session_id,
                    )
                    runner_client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _runner_control_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                    )
                # Runner is dead or still not spawned for a host-bound
                # session. Ask the host to launch one, then re-fetch the
                # runner client and wait briefly for it to connect before
                # forwarding the message. This is the relaunch path a
                # non-sticky Stop relies on: after Stop drops the runner
                # tunnel, the next message lands here and relaunches the
                # session on its still-online host. Gated only on host
                # presence — if the host is offline this falls through to
                # the RUNNER_UNAVAILABLE raise below, the same as a
                # disconnected CLI session.
                _host_reg = getattr(request.app.state, "host_registry", None)
                # Set when a non-acking (wedged) host tunnel is evicted below, so we
                # skip the blind connect wait and fail fast (BDP-2491).
                _host_evicted = False
                if runner_client is None and _host_reg is not None:
                    _host_conn = _host_reg.get(conv.host_id)
                    if _host_conn is not None:
                        launch_attempt = await _launch_runner_on_host(
                            conv,
                            conversation_store,
                            _host_reg,
                            _host_conn,
                            owner=user_id,
                            runner_control_registry=_runner_control_registry,
                            runner_credential_store=getattr(
                                request.app.state,
                                "runner_credential_store",
                                None,
                            ),
                        )
                        if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                            # The host refused: the agent's harness isn't
                            # configured there. This message was the real
                            # runner-start attempt, so consume it and record a
                            # transcript error (the host's message names the
                            # fix, `omnigent setup`) the web renders as a
                            # banner — instead of timing out into a generic
                            # RUNNER_UNAVAILABLE. The binding stays so a later
                            # message relaunches once setup is done.
                            item_id = await _persist_host_launch_failure_turn(
                                session_id,
                                conv,
                                body,
                                conversation_store,
                                launch_attempt.error,
                                runner_router,
                                created_by=_attribution_user(user_id),
                            )
                            return {"queued": True, "item_id": item_id}
                        if not launch_attempt.acked:
                            # The host never ACKed the launch within the budget:
                            # its tunnel is registered but is not delivering
                            # ``host.launch_runner`` frames into dispatch (the
                            # wedged-host failure mode, BDP-2491). Evict it so its
                            # reconnect loop rebuilds a deliverable tunnel, and fail
                            # fast (skip the blind 30s connect wait below) — the
                            # user's next message relaunches on the fresh tunnel,
                            # instead of every retry waiting out the full timeout
                            # until someone restarts the host by hand.
                            if _host_reg.evict(_host_conn):
                                _logger.warning(
                                    "Evicted non-acking host %s for session %s "
                                    "(launch ACK timeout) — host will reconnect",
                                    conv.host_id,
                                    session_id,
                                )
                            _host_evicted = True
                            relaunched_runner_id = None
                        else:
                            relaunched_runner_id = launch_attempt.runner_id
                    else:
                        launch_attempt = await _launch_runner_on_host_id(
                            conv,
                            conversation_store,
                            _host_reg,
                            conv.host_id,
                            owner=user_id,
                            runner_control_registry=_runner_control_registry,
                            runner_credential_store=getattr(
                                request.app.state,
                                "runner_credential_store",
                                None,
                            ),
                        )
                        if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                            item_id = await _persist_host_launch_failure_turn(
                                session_id,
                                conv,
                                body,
                                conversation_store,
                                launch_attempt.error,
                                runner_router,
                                created_by=_attribution_user(user_id),
                            )
                            return {"queued": True, "item_id": item_id}
                        if launch_attempt.acked:
                            relaunched_runner_id = launch_attempt.runner_id
                        else:
                            relaunched_runner_id = None
                            _host_evicted = True
                            # The host tunnel is gone entirely. A managed
                            # host's sandbox is relaunchable — provision a new
                            # generation under the same host identity and ride
                            # it; an external (laptop) host falls through to
                            # the unavailable raise below.
                            if await _maybe_relaunch_managed_sandbox(
                                session_id=session_id,
                                conv=conv,
                                app_state=request.app.state,
                                conversation_store=conversation_store,
                            ):
                                conv_after_relaunch = await asyncio.to_thread(
                                    conversation_store.get_conversation, session_id
                                )
                                if conv_after_relaunch is None:
                                    raise OmnigentError(
                                        "Session not found",
                                        code=ErrorCode.NOT_FOUND,
                                    )
                                conv = conv_after_relaunch
                                runner_client = await _get_runner_client(session_id, runner_router)
                else:
                    relaunched_runner_id = None
                if runner_client is None and not _host_evicted:
                    _logger.info(
                        "Waiting up to %.0fs for host %s to spawn a runner for session %s",
                        _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                        conv.host_id,
                        session_id,
                    )
                    runner_client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _runner_control_registry,
                        runner_id=relaunched_runner_id,
                        timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                        runner_exit_reports=runner_exit_reports,
                    )
                if runner_client is None:
                    _runner_needs_session_init = False
                else:
                    _runner_needs_session_init = True
            if runner_client is None:
                # A native terminal-session message must NOT be silently
                # dropped when no runner is reachable — the runner crashed
                # before connecting (the daemon couldn't bring it up). Persist
                # the user's message together with the runner-failure error so
                # it survives reload and the banner explains why, becoming the
                # AP-server-as-writer failed turn (same shape as a definitive
                # ensure-probe failure). The cause, when known, is the daemon's
                # exit report keyed by this session's runner_id; otherwise a
                # generic unavailable message. This is safe precisely because
                # the harness will never see it (no desync — there is no live
                # harness). Other event types and non-native sessions still
                # raise: their message would replay to a relaunched runner, so
                # persisting now WOULD desync the store from harness state.
                if body.type == "message" and _is_native_terminal_session(conv):
                    exit_cause = (
                        runner_exit_reports.get(conv.runner_id)
                        if runner_exit_reports is not None and conv.runner_id is not None
                        else None
                    )
                    offline_error = ErrorData(
                        source="execution",
                        code="runner_failed_to_start",
                        message=(
                            exit_cause
                            if exit_cause
                            else (
                                "The runner for this session is not available — "
                                "it may have failed to start. See the host logs."
                            )
                        ),
                    )
                    item_id = await _persist_native_terminal_failure(
                        session_id,
                        conv,
                        body,
                        conversation_store,
                        offline_error,
                        runner_router,
                        created_by=_attribution_user(user_id),
                    )
                    return {"queued": True, "item_id": item_id}
                # Raise so the Omnigent server doesn't persist an item the
                # harness will never see. Other event paths (interrupt,
                # approval) are best-effort and silently skip when no
                # runner is bound — item events can't, because that
                # would desync conversation store and harness state.
                raise OmnigentError(
                    "No runner bound for session",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            refreshed_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if refreshed_conv is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
            conv = refreshed_conv
            _child_user_message_event = _is_child_user_message_event(conv, body)
            if _runner_needs_session_init or _child_user_message_event:
                # The runner was unavailable when this request began, so its
                # connect callback may still be racing us. Await the handshake
                # so the terminal + transcript forwarder are watching before we
                # inject the message — otherwise a native web message is
                # forwarded into a TUI whose forwarder isn't attached, the
                # round-trip never mirrors back, and the optimistic bubble
                # sticks with no reply (host-restart bug).
                await _ensure_runner_session_initialized(session_id, conv, runner_client)
            if _child_user_message_event:
                _logger.info(
                    "Skipping child session relay-ready wait before forwarding message session=%s",
                    session_id,
                )
            else:
                # Self-heal a dead/ephemeral runner on the message path: relaunch +
                # re-resolve conv/client + retry the relay handshake once, instead of
                # 503-ing the user's message (BDP-2601). Internal callers have no
                # ``request`` and keep the one-shot behavior.
                conv, runner_client = await _ensure_runner_relay_ready_with_heal(
                    session_id,
                    request,
                    conv,
                    runner_client,
                    conversation_store,
                    runner_router,
                )
            _agent = agent_store.get(conv.agent_id) if conv.agent_id else None
            # Determine whether the agent has MCP servers so the runner's
            # proxy_stream handler knows to initialise ProxyMcpManager.
            # agent_cache.load() is O(1) on a warm in-memory cache; the
            # asyncio.to_thread wrapper covers the rare cold-cache path
            # where the bundle is extracted from disk for the first time.
            _has_mcp_servers = False
            if _agent is not None and agent_cache is not None and _agent.bundle_location:
                try:
                    _loaded_agent = await asyncio.to_thread(
                        agent_cache.load,
                        _agent.id,
                        _agent.bundle_location,
                    )
                    _has_mcp_servers = bool(_loaded_agent.spec.mcp_servers)
                except Exception:  # noqa: BLE001 — spec load failure must not break event forwarding
                    _logger.warning(
                        "Failed to load agent spec for MCP hint for session=%s",
                        session_id,
                        exc_info=True,
                    )
            if body.type == _SLASH_COMMAND_TYPE:
                if _agent is None:
                    raise OmnigentError(
                        f"Session {session_id!r} has no agent; cannot run slash command",
                        code=ErrorCode.INVALID_INPUT,
                    )
                item_id = await _dispatch_skill_slash_command_to_runner(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    runner_client,
                    agent=_agent,
                    has_mcp_servers=_has_mcp_servers,
                    created_by=_attribution_user(user_id),
                )
                return {"queued": True, "item_id": item_id}
            dispatch = await _dispatch_session_event_to_runner(
                session_id,
                conv,
                body,
                conversation_store,
                runner_client,
                agent_name=_agent.name if _agent else None,
                file_store=file_store,
                artifact_store=artifact_store,
                has_mcp_servers=_has_mcp_servers,
                created_by=_attribution_user(user_id),
                runner_router=runner_router,
            )
            response: dict[str, Any] = {"queued": True}
            if dispatch.item_id is not None:
                response["item_id"] = dispatch.item_id
            # Native-terminal web message: hand back the pending-input id. It
            # identifies the snapshot's replayed bubble on rebind and is the
            # cleared_pending_id the consume event carries to drop it. Clients
            # may adopt it onto their optimistic bubble for id-based dedupe;
            # the first-party web client keeps its client temp id (React-key
            # stability) and relies on stableKey + FIFO instead.
            if dispatch.pending_id is not None:
                response["pending_id"] = dispatch.pending_id
            return response

        async def _try_handle_blueprint_session_event(
            *,
            request: Request,
            session_id: str,
            conv: Conversation,
            body: SessionEventInput,
            user_id: str | None,
        ) -> dict[str, bool | str] | None:
            """
            Handle a user message for an ``executor.type: blueprint`` session.

            Returns ``None`` for ordinary agents so the normal runner dispatch path
            continues unchanged.
            """
            if body.type != "message" or body.data.get("role") != "user" or conv.agent_id is None:
                return None
            loaded = await _load_agent_spec_for_conversation(conv)
            if loaded is None or loaded.spec.executor.type != "blueprint":
                return None
            if loaded.spec.blueprint is None:
                raise OmnigentError(
                    "Blueprint executor requires a blueprint block",
                    code=ErrorCode.INVALID_INPUT,
                )

            response_id = f"bpr_input_{secrets.token_hex(8)}"
            user_item = NewConversationItem(
                type="message",
                response_id=response_id,
                data=parse_item_data("message", {"type": "message", **body.data}),
                created_by=_attribution_user(user_id),
            )
            persisted_items = await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [user_item],
            )
            accepted = persisted_items[0]
            _publish_input_consumed(session_id, accepted)

            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            agent_name = agent.name if agent is not None else loaded.spec.name or "blueprint"
            _publish_status(session_id, "running")

            async def emit(event: dict[str, Any]) -> None:
                event_item = NewConversationItem(
                    type="blueprint_event",
                    response_id=str(event["blueprint_run_id"]),
                    data=BlueprintEventData(**event),
                )
                await asyncio.to_thread(conversation_store.append, session_id, [event_item])

            async def dispatch_child(
                node: BlueprintNode,
                context: dict[str, Any],
                loop_iteration: int | None,
            ) -> ChildDispatchResult:
                return await _dispatch_blueprint_child_node(
                    request=request,
                    parent_session_id=session_id,
                    parent_user_id=user_id,
                    node=node,
                    context=context,
                    loop_iteration=loop_iteration,
                )

            runner = BlueprintRunner(
                loaded.spec.blueprint,
                emit=emit,
                dispatch_child=dispatch_child,
            )
            result = await runner.run(_message_body_to_blueprint_input(body.data))
            if result.status == "failed":
                _publish_status(
                    session_id,
                    "failed",
                    ErrorDetail(
                        code="blueprint_failed",
                        message=result.error or "Blueprint execution failed",
                    ),
                )
                return {"queued": True, "item_id": accepted.id}
            if result.status == "waiting":
                _publish_status(session_id, "waiting")
                return {"queued": True, "item_id": accepted.id}

            assistant_item = NewConversationItem(
                type="message",
                response_id=result.blueprint_run_id,
                data=MessageData(
                    role="assistant",
                    agent=agent_name,
                    content=[
                        {
                            "type": "output_text",
                            "text": _blueprint_output_to_text(result.output),
                        }
                    ],
                ),
            )
            assistant_items = await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [assistant_item],
            )
            _publish_external_conversation_item(session_id, assistant_items[0])
            _publish_status(session_id, "idle")
            return {"queued": True, "item_id": accepted.id}

        async def _load_agent_spec_for_conversation(conv: Conversation) -> Any | None:
            """Load the parsed spec for a conversation's bound agent."""
            if conv.agent_id is None or agent_cache is None:
                return None
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is None:
                return None
            return await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
                expand_env=agent.session_id is None,
            )

        def _blueprint_child_delegation_service(
            *,
            request: Request,
            node: BlueprintNode,
            target_agent: Agent,
        ) -> ChildSessionDelegationService:
            """Build the child-session delegation service for a blueprint node."""

            async def create_child_session(command: DelegateToAgentCommand) -> str:
                if command.agent_id is None:
                    raise OmnigentError(
                        f"Blueprint node {node.id!r} is missing a target agent id",
                        code=ErrorCode.INVALID_INPUT,
                    )
                child = await _create_session_from_existing_agent(
                    conversation_store,
                    agent_store,
                    runner_router,
                    SessionCreateRequest(
                        agent_id=command.agent_id,
                        parent_session_id=command.parent_session_id,
                        title=command.title,
                        labels=dict(command.labels),
                    ),
                    request,
                    agent_cache=agent_cache,
                    user_id=command.actor.user_id,
                    permission_store=permission_store,
                    liveness_lookup=liveness_lookup,
                )
                return child.id

            async def post_child_prompt(
                child_session_id: str,
                prompt: str,
                actor: ChatActor,
            ) -> None:
                del actor
                await post_event(
                    request,
                    child_session_id,
                    SessionEventInput(
                        type="message",
                        data={
                            "role": "user",
                            "content": [{"type": "input_text", "text": prompt}],
                        },
                    ),
                )

            async def record_child_return(
                command: DelegateToAgentCommand,
                child_session_id: str,
                raw_output: str,
            ) -> None:
                await _append_blueprint_child_return(
                    command.parent_session_id,
                    node=node,
                    child_session_id=child_session_id,
                    target=command.agent_name or target_agent.name,
                    output=raw_output,
                )

            def parse_child_output(
                command: DelegateToAgentCommand,
                raw_output: str,
            ) -> tuple[Literal["completed", "failed"], Any, str | None]:
                del command
                return _blueprint_child_output(node, raw_output)

            return ChildSessionDelegationService(
                create_child_session=create_child_session,
                post_child_prompt=post_child_prompt,
                read_child_output=_latest_assistant_text,
                record_child_return=record_child_return,
                parse_child_output=parse_child_output,
                is_runner_unavailable=lambda exc: (
                    isinstance(exc, OmnigentError) and exc.code == ErrorCode.RUNNER_UNAVAILABLE
                ),
            )

        async def _dispatch_blueprint_child_node(
            *,
            request: Request,
            parent_session_id: str,
            parent_user_id: str | None,
            node: BlueprintNode,
            context: dict[str, Any],
            loop_iteration: int | None,
        ) -> ChildDispatchResult:
            """Create and optionally drive a child session for a blueprint node."""
            target_agent = await _resolve_blueprint_target_agent(node)
            target_loaded = await asyncio.to_thread(
                agent_cache.load,
                target_agent.id,
                target_agent.bundle_location,
                expand_env=target_agent.session_id is None,
            ) if agent_cache is not None else None
            if node.kind == "blueprint":
                if target_agent.category != "workflow" and not (
                    target_loaded is not None and target_loaded.spec.executor.type == "blueprint"
                ):
                    return ChildDispatchResult(
                        status="failed",
                        error="kind 'blueprint' may target only workflow-category agents",
                    )

            rendered_input = render_blueprint_value(node.input, context)
            prompt = _child_prompt_text(rendered_input)
            command = DelegateToAgentCommand(
                parent_session_id=parent_session_id,
                actor=ChatActor(kind=ChatActorKind.USER, user_id=parent_user_id),
                agent_id=target_agent.id,
                agent_name=target_agent.name,
                title=f"{node.kind}:{node.id}",
                prompt=prompt,
                labels={
                    "omnigent.blueprint.node_id": node.id,
                    "omnigent.blueprint.node_kind": node.kind,
                    "omnigent.blueprint.target": target_agent.name,
                    "omnigent.blueprint.loop_iteration": (
                        str(loop_iteration) if loop_iteration is not None else ""
                    ),
                },
                metadata={
                    "blueprint_node_id": node.id,
                    "blueprint_node_kind": node.kind,
                    "blueprint_target": target_agent.name,
                    "blueprint_loop_iteration": loop_iteration,
                },
            )
            outcome = await _blueprint_child_delegation_service(
                request=request,
                node=node,
                target_agent=target_agent,
            ).delegate(command)
            return ChildDispatchResult(
                status=outcome.status,
                child_session_id=outcome.child_session_id,
                output=outcome.output,
                error=outcome.error,
            )

        async def _append_blueprint_child_return(
            parent_session_id: str,
            *,
            node: BlueprintNode,
            child_session_id: str,
            target: str,
            output: str,
        ) -> None:
            """Persist one hidden parent-inbox style result for a child node."""
            text = (
                f"[System: blueprint node {node.id} completed — "
                f"{target} returned from child session {child_session_id}: {output}]"
            )
            item = NewConversationItem(
                type="message",
                response_id=f"bpr_child_{secrets.token_hex(8)}",
                data=MessageData(
                    role="assistant",
                    agent="blueprint",
                    content=[{"type": "output_text", "text": text}],
                    is_meta=True,
                ),
            )
            await asyncio.to_thread(conversation_store.append, parent_session_id, [item])

        async def _resolve_blueprint_target_agent(node: BlueprintNode) -> Agent:
            """Resolve a blueprint node target by id first, then template name."""
            if not node.target:
                raise OmnigentError(
                    f"Blueprint node {node.id!r} is missing a target",
                    code=ErrorCode.INVALID_INPUT,
                )
            agent = await asyncio.to_thread(agent_store.get, node.target)
            if agent is None:
                agent = await asyncio.to_thread(agent_store.get_by_name, node.target)
            if agent is None:
                raise OmnigentError(
                    f"Blueprint target not found: {node.target!r}",
                    code=ErrorCode.NOT_FOUND,
                )
            return agent

        async def _latest_assistant_text(session_id: str) -> str:
            """Return the newest assistant message text for a child session."""
            page = await asyncio.to_thread(
                conversation_store.list_items,
                session_id,
                50,
                None,
                None,
                "desc",
                "message",
            )
            for item in page.data:
                if isinstance(item.data, MessageData) and item.data.role == "assistant":
                    return _message_blocks_to_text(item.data.content)
            return ""

        def _message_body_to_blueprint_input(data: dict[str, Any]) -> dict[str, Any]:
            """Project a user message payload into blueprint input context."""
            content = data.get("content")
            blocks = content if isinstance(content, list) else []
            text = _message_blocks_to_text(blocks)
            return {"message": data, "content": blocks, "text": text}

        def _message_blocks_to_text(blocks: list[dict[str, Any]]) -> str:
            """Extract plain text from message content blocks."""
            parts: list[str] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)

        def _child_prompt_text(value: Any) -> str:
            """Serialize rendered child input into a message prompt."""
            if isinstance(value, str):
                return value
            return json.dumps(value if value is not None else {}, indent=2, sort_keys=True)

        def _blueprint_child_output(
            node: BlueprintNode,
            raw_output: str,
        ) -> tuple[Literal["completed", "failed"], Any, str | None]:
            """
            Apply optional strict output parsing for child blueprint/agent nodes.

            ``metadata.expect_json: true`` is deliberately opt-in so existing
            child-session blueprint behavior stays text-compatible.
            """
            if not _blueprint_metadata_flag(node, "expect_json"):
                return "completed", raw_output, None
            parsed = _parse_blueprint_child_json_object(raw_output)
            if parsed is None:
                return (
                    "failed",
                    {
                        "approved": False,
                        "error": "invalid_child_json",
                        "raw_text": raw_output,
                    },
                    f"Blueprint node {node.id!r} expected a JSON object from child output",
                )
            return "completed", parsed, None

        def _blueprint_metadata_flag(node: BlueprintNode, key: str) -> bool:
            value = node.metadata.get(key)
            if value is True:
                return True
            return isinstance(value, str) and value.strip().lower() == "true"

        _BLUEPRINT_JSON_FENCE_RE = re.compile(
            r"^```(?:json)?\s*(.*?)\s*```$",
            re.IGNORECASE | re.DOTALL,
        )

        def _parse_blueprint_child_json_object(raw_output: str) -> dict[str, Any] | None:
            """Parse raw or fenced JSON object text returned by a child node."""
            candidate = raw_output.strip()
            match = _BLUEPRINT_JSON_FENCE_RE.fullmatch(candidate)
            if match:
                candidate = match.group(1).strip()
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None

        def _blueprint_output_to_text(value: Any) -> str:
            """Serialize a blueprint output into assistant message text."""
            if isinstance(value, str):
                return value
            if value is None:
                return ""
            return json.dumps(value, indent=2, sort_keys=True)

        @router.get(
            "/sessions/{session_id}/blueprint-run",
            response_model=BlueprintRunResponse,
        )
        async def get_blueprint_run(
            request: Request,
            session_id: str,
        ) -> BlueprintRunResponse:
            """Return live blueprint run state reconstructed from durable events."""
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id,
                session_id,
                LEVEL_READ,
                permission_store,
                conversation_store,
            )
            if access.conversation is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
            page = await asyncio.to_thread(
                conversation_store.list_items,
                session_id,
                1000,
                None,
                None,
                "asc",
                "blueprint_event",
            )
            return BlueprintRunResponse.model_validate(blueprint_events_to_run(page.data))
