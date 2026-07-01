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

def register_session_policy_evaluate(
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
        # ── POST /sessions/{session_id}/policies/evaluate ─────────────

        @router.post(
            "/sessions/{session_id}/policies/evaluate",
            # Returns EvaluationResponse JSON; no Pydantic model since the
            # proto-style schema is validated manually.
            response_model=None,
            # CSRF hardening: body is parsed via request.json(); require a JSON
            # Content-Type so a cross-site text/plain request can't reach it.
            dependencies=[Depends(require_json_content_type)],
        )
        async def evaluate_policy(
            request: Request,
            session_id: str,
        ) -> Response:
            """
            Generic policy evaluation endpoint (proto-compatible).

            Accepts an ``EvaluationRequest`` JSON body whose ``event``
            field carries the phase (``PHASE_TOOL_CALL``,
            ``PHASE_TOOL_RESULT``, ``PHASE_LLM_REQUEST``,
            ``PHASE_LLM_RESPONSE``), the event data, and optional
            context. Returns an ``EvaluationResponse`` with the policy
            verdict (``result``), an optional ``reason``, and optional
            ``data`` for content-rewriting policies.

            Used by Claude Code's ``PreToolUse`` and ``PostToolUse``
            command hooks (via ``omnigent.claude_native_hook``) to
            evaluate admin policies on native tool calls. Also usable
            by any client that speaks the proto-compatible JSON schema.

            :param request: FastAPI request — body is the
                ``EvaluationRequest`` JSON envelope.
            :param session_id: Omnigent conversation id from the URL path.
            :returns: ``EvaluationResponse`` JSON with ``result``,
                ``reason``, and optional ``data``.
            :raises OmnigentError: 404 if the session doesn't exist,
                400 if the body is malformed.
            """
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            is_read_only = access.level is not None and access.level < LEVEL_EDIT
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                raise OmnigentError(
                    f"Invalid JSON in policy evaluate body: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            if not isinstance(payload, dict):
                raise OmnigentError(
                    "Policy evaluate body must be a JSON object.",
                    code=ErrorCode.INVALID_INPUT,
                )
            event = payload.get("event")
            if not isinstance(event, dict):
                raise OmnigentError(
                    "Policy evaluate body must include an 'event' object.",
                    code=ErrorCode.INVALID_INPUT,
                )
            event_type = event.get("type")
            phase = _PROTO_EVENT_TYPE_TO_PHASE.get(event_type or "")
            if phase is None:
                raise OmnigentError(
                    f"Unknown event type: {event_type!r}. "
                    f"Expected one of {list(_PROTO_EVENT_TYPE_TO_PHASE)}.",
                    code=ErrorCode.INVALID_INPUT,
                )
            data = event.get("data") or {}

            conv = conversation_store.get_conversation(session_id)
            if conv is None:
                raise OmnigentError(
                    f"Session {session_id!r} not found.",
                    code=ErrorCode.NOT_FOUND,
                )
            # Dedup the native request-phase gate. A native session's
            # ``UserPromptSubmit`` hook posts ``PHASE_REQUEST`` here for *every*
            # prompt, but a web-UI prompt was already gated server-side by
            # ``_evaluate_input_policy`` at POST /events (before injection, so no
            # TUI freeze). Re-gating it here would double-prompt the human. A
            # web-UI prompt in flight has a ``pending_inputs`` entry (recorded at
            # dispatch, drained when the forwarder mirrors it back); a prompt
            # typed directly in the TUI has none and never hit POST /events, so it
            # is gated here — the hook is its only request-phase gate. The signal
            # is "is a web prompt in flight", not text correlation (the native
            # transcript gives no reliable id channel — see ``pending_inputs``).
            if phase == Phase.REQUEST and pending_inputs.snapshot_for(session_id):
                return Response(
                    content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                    media_type="application/json",
                )
            agent = agent_store.get(conv.agent_id) if conv.agent_id else None
            if agent is None:
                # No agent — no policies. Return unspecified (pass-through).
                return Response(
                    content=json.dumps({"result": "POLICY_ACTION_UNSPECIFIED"}),
                    media_type="application/json",
                )

            loaded = get_agent_cache().load(
                agent.id, agent.bundle_location, expand_env=agent.session_id is None
            )

            _caps = get_caps()
            _host_conn = (
                _caps.policy_llm_connection_factory() if _caps.policy_llm_connection_factory else None
            )

            def _build_engine() -> PolicyEngine:
                """
                Build a policy engine for this session from the loaded spec.

                Re-reads persisted ``session_state`` / usage from the store on
                every call: the engine snapshots that state at construction and
                does not re-query it during ``evaluate``, so a fresh build is the
                only way to observe a concurrent sibling's just-recorded approval.

                :returns: A :class:`PolicyEngine` seeded with the latest
                    persisted state for ``session_id``.
                """
                return build_policy_engine(
                    spec=loaded.spec,
                    conversation_id=session_id,
                    conversation_store=conversation_store,
                    default_policies=_caps.default_policies,
                    policy_store=get_policy_store(),
                    server_llm=_caps.llm,
                    host_connection=_host_conn,
                )

            engine = _build_engine()
            ctx = _build_evaluation_context(phase, data, event, actor=_build_actor(user_id))
            result = await engine.evaluate(ctx, read_only=is_read_only)

            # URL-based elicitation for blocking phases: on a TOOL_CALL or
            # LLM_REQUEST ASK, hold the gate server-side rather than
            # returning ASK. Returning ASK makes the native hook emit
            # ``defer``, which a permissive ``permission_mode``
            # (acceptEdits / bypassPermissions) auto-approves — bypassing
            # the human. Instead we publish the approval elicitation, park
            # until the human resolves it via the resolve URL, and collapse
            # to a hard ALLOW / DENY so the caller never sees ASK.
            # TOOL_CALL, LLM_REQUEST, and REQUEST are the phases that can block
            # before the action proceeds (tool dispatch / LLM call / a native
            # session's user prompt via the UserPromptSubmit hook — which has no
            # ASK primitive of its own, so the server resolves ASK here).
            if result.action == PolicyAction.ASK and phase in (
                Phase.TOOL_CALL,
                Phase.LLM_REQUEST,
                Phase.REQUEST,
            ):
                if is_read_only:
                    # Read-only callers must not enter the ASK gate — parking
                    # creates an elicitation (a server-side mutation). Return
                    # the ASK verdict directly so the caller sees the policy
                    # decision without mutating the session.
                    pass
                else:
                    # Serialize concurrent native ASK gates for this (session, policy)
                    # so parallel tool calls that all trip the same checkpoint prompt
                    # the human once. The first ASK to win the lock parks; on approve
                    # it records a checkpoint. Siblings then rebuild the engine and
                    # re-evaluate UNDER the lock against that freshly persisted state —
                    # an ALLOW (or now-hard DENY) collapses the ASK and falls through
                    # without a second prompt. Held across the human wait by design;
                    # a declined ASK records nothing, so siblings legitimately re-ask.
                    async with _native_ask_gate_lock(session_id, result.deciding_policy):
                        engine = _build_engine()
                        result = await engine.evaluate(ctx, read_only=is_read_only)
                        if result.action == PolicyAction.ASK and phase in (
                            Phase.TOOL_CALL,
                            Phase.LLM_REQUEST,
                            Phase.REQUEST,
                        ):
                            approved = await _hold_native_ask_gate(
                                request,
                                session_id=session_id,
                                phase=phase,
                                data=data,
                                engine=engine,
                                result=result,
                                conversation_store=conversation_store,
                            )
                            verdict_body: dict[str, Any] = (
                                {"result": "POLICY_ACTION_ALLOW"}
                                if approved
                                else {
                                    "result": "POLICY_ACTION_DENY",
                                    "reason": result.reason or "Approval was not granted.",
                                }
                            )
                            return Response(
                                content=json.dumps(verdict_body),
                                media_type="application/json",
                            )
                    # Re-evaluation collapsed the ASK (a sibling's approval recorded
                    # the checkpoint) — fall through to the generic ALLOW/DENY handling
                    # below with the rebuilt engine and updated result.

            if result.set_labels and not is_read_only:
                engine.apply_label_writes(result.set_labels)

            resp_body: dict[str, Any] = {
                "result": _PHASE_TO_PROTO_ACTION.get(result.action, "POLICY_ACTION_UNSPECIFIED"),
            }
            if result.reason:
                resp_body["reason"] = result.reason
            if result.data is not None:
                resp_body["data"] = result.data
            return Response(
                content=json.dumps(resp_body),
                media_type="application/json",
            )

