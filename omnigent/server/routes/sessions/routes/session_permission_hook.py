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

def register_session_permission_hook(
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
        # ── POST /sessions/{session_id}/hooks/permission-request ─────

        @router.post(
            "/sessions/{session_id}/hooks/permission-request",
            response_model=None,
            # CSRF hardening: body is parsed via request.json(); require a JSON
            # Content-Type so a cross-site text/plain request can't reach it.
            dependencies=[Depends(require_json_content_type)],
        )
        async def claude_permission_request_hook(
            request: Request,
            session_id: str,
        ) -> Response:
            """
            Claude Code ``PermissionRequest`` HTTP hook endpoint.

            Receives Claude Code's PermissionRequest hook payload (tool
            name + input the user would otherwise see a TUI prompt for),
            publishes a ``response.elicitation_request`` SSE event on the
            session stream so the web UI's :file:`ApprovalCard` renders
            inline, and long-polls until the verdict arrives via the
            session ``approval`` event path.

            Response shape follows Claude Code's PermissionRequest hook
            contract: ``hookSpecificOutput.decision.behavior`` is
            ``"allow"`` or ``"deny"``. On timeout the endpoint returns
            ``200`` with an empty body — Claude Code treats that as
            "defer to the TUI prompt", which matches the wrapper's
            fail-ask contract (UI unreachable / unattended → fall back
            to terminal-side approval).

            Auth: standard session ACL — the wrapper's outbound headers
            (``ap_auth_headers`` in :func:`build_hook_settings`) carry
            the same Bearer token used for every other Omnigent request. For
            local-server mode (no auth provider), unauth'd calls are
            allowed.

            :param request: FastAPI request — body is Claude Code's
                PermissionRequest payload as JSON.
            :param session_id: Omnigent conversation id from the URL path.
            :returns: Claude PermissionRequest hookSpecificOutput JSON,
                or ``200`` with empty body on timeout (fail-ask).
            :raises OmnigentError: 404 if the session doesn't exist,
                400 if the body fails JSON parse or is missing
                ``tool_name``.
            """
            user_id = _get_user_id(request, auth_provider)
            await _require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                raise OmnigentError(
                    f"Invalid JSON in PermissionRequest hook body: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            if not isinstance(payload, dict):
                raise OmnigentError(
                    "PermissionRequest hook body must be a JSON object.",
                    code=ErrorCode.INVALID_INPUT,
                )
            tool_name = payload.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                raise OmnigentError(
                    "PermissionRequest hook body must include a non-empty 'tool_name' string.",
                    code=ErrorCode.INVALID_INPUT,
                )
            tool_input = payload.get("tool_input")
            if tool_input is not None and not isinstance(tool_input, dict):
                raise OmnigentError(
                    "PermissionRequest hook body 'tool_input' must be an object when present.",
                    code=ErrorCode.INVALID_INPUT,
                )
            # ``tool_use_id`` is not stable on Claude Code's
            # PermissionRequest payload, and newer builds can write the
            # transcript ``function_call`` (tool_use) before this hook
            # returns — so neither can correlate/resolve the parked
            # request. The parked wait ends on one of three signals: an
            # explicit web verdict, hook disconnect, or the mirrored
            # ``function_call_output`` (tool_result) for this gated tool,
            # which — unlike the tool_use — is written only AFTER the
            # prompt was answered in the TUI. We pass ``tool_name`` /
            # ``tool_input`` below so that result can be correlated back to
            # THIS prompt (see _signal_terminal_resolved_harness_elicitation).
            cwd = payload.get("cwd")
            if cwd is not None and not isinstance(cwd, str):
                cwd = None
            permission_mode = payload.get("permission_mode")
            if permission_mode is not None and not isinstance(permission_mode, str):
                permission_mode = None
            elicitation_id = _client_supplied_hook_elicitation_id(payload, session_id)

            try:
                preview_str = json.dumps(tool_input or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                preview_str = repr(tool_input)
            preview_str = preview_str[:1024]

            # ``extra="allow"`` on ElicitationRequestParams permits
            # extra keyword arguments to ride alongside the MCP
            # standard fields. Use it for Claude-native display and
            # correlation hints rather than minting AP-specific fields
            # on the model; strict MCP clients can ignore unknown fields
            # while AP's UI consumes them.
            # ``tool_name`` rides along so the UI can render the
            # permission card with the gated tool name and distinguish
            # simultaneous prompts from different tools.
            extras: dict[str, Any] = {"tool_name": tool_name}
            if cwd is not None:
                extras["cwd"] = cwd
            if permission_mode is not None:
                extras["permission_mode"] = permission_mode
            # Stamp the "Accept & allow all edits" hint (drives the UI's
            # third button) only for edit-tool prompts under a still-prompting
            # mode — see _allow_all_edits_eligible. The verdict site re-checks
            # the same predicate before honoring the flag.
            if _allow_all_edits_eligible(tool_name, permission_mode):
                extras["allow_all_edits"] = True
            # When Claude's built-in AskUserQuestion tool is the one
            # needing permission, the PermissionRequest payload
            # already carries the full questions + options structure
            # in ``tool_input``. Surface it as a structured extra so
            # the UI can render an interactive form WITHOUT having to
            # parse the (truncated) ``content_preview`` JSON blob.
            # ``content_preview`` keeps its 1024-char cap for the
            # binary-card fallback; the structured field is the
            # authoritative source the UI consumes when present.
            if tool_name == "AskUserQuestion":
                ask_payload = _structured_ask_user_question(tool_input)
                if ask_payload is not None:
                    extras["ask_user_question"] = ask_payload
            # When the gated tool is ExitPlanMode, ride the full
            # ``tool_input`` through verbatim so the UI can render a
            # dedicated plan-review card. ``content_preview`` is
            # hard-capped at 1024 chars — real plans blow well past it —
            # and the input's shape varies across Claude Code builds
            # (``plan`` markdown, ``allowedPrompts``, ...), so no field
            # filtering: every field the hook carried natively reaches
            # the UI. An empty/absent input stamps nothing, leaving the
            # binary-card fallback.
            if tool_name == "ExitPlanMode" and isinstance(tool_input, dict) and tool_input:
                extras["exit_plan_mode"] = tool_input
            params = ElicitationRequestParams(
                mode="form",
                message=f"Claude wants to call **{tool_name}**",
                requestedSchema=None,
                url=None,
                phase="pre_tool_use",
                policy_name="claude_native_permission",
                content_preview=f"{tool_name}({preview_str})",
                **extras,
            )
            result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=session_id,
                params=params,
                timeout_s=_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
                conversation_store=conversation_store,
                # Client-minted stable id so a retry re-parks the same elicitation.
                elicitation_id=elicitation_id,
                # Tool identity lets a mirrored tool result for this gated
                # tool resolve the prompt promptly when the user answers in
                # Claude's TUI instead of the web UI (terminal-resolved
                # fast path). ``tool_input`` is the dict from the payload
                # (or None when absent).
                tool_name=tool_name,
                tool_input=tool_input if isinstance(tool_input, dict) else None,
            )
            if result is None:
                # Disconnect or timeout. Either way Claude is no
                # longer waiting on this response; empty 2xx → Claude
                # defers to its built-in TUI prompt (fail-ask).
                return Response(status_code=status.HTTP_200_OK)

            behavior = "allow" if result.action == "accept" else "deny"
            decision: dict[str, Any] = {"behavior": behavior}
            # A decline can carry feedback typed into the web card (the
            # ExitPlanMode "Reject with feedback" flow). Claude's
            # PermissionRequest decision contract surfaces it via
            # ``decision.message`` — the model sees it as the denial
            # reason, so for a rejected plan Claude stays in plan mode
            # and revises toward the feedback instead of guessing why
            # the plan was refused.
            if behavior == "deny" and isinstance(result.content, dict):
                feedback = result.content.get("feedback")
                if isinstance(feedback, str) and feedback.strip():
                    decision["message"] = feedback
            # When the gated tool is AskUserQuestion AND the user accepted
            # with selections, propagate those selections back to Claude
            # via ``decision.updatedInput``. Claude reads
            # ``tool_input.answers`` and skips its TUI picker, returning
            # the supplied selections as the tool result the LLM sees.
            #
            # ``result.content`` is MCP-shaped (a flat ``{[field]: value}``
            # map) — exactly the shape ``tool_input.answers`` expects on
            # AskUserQuestion. Single-select values are strings,
            # multi-select are ``list[str]``; both ride through verbatim.
            if (
                behavior == "allow"
                and tool_name == "AskUserQuestion"
                and isinstance(tool_input, dict)
                and isinstance(result.content, dict)
                and result.content
            ):
                decision["updatedInput"] = {**tool_input, "answers": result.content}
            # "Accept & allow all edits" — the user approved this edit AND
            # asked to auto-accept future edits. Echo a ``setMode`` permission
            # update so Claude Code switches this session into ``acceptEdits``
            # mode, exactly as the native shift+tab toggle does. The
            # ``updatedPermissions`` shape matches the Agent SDK's
            # ``PermissionUpdate`` union (``{type, mode, destination}`` for
            # ``setMode``); ``destination: "session"`` scopes it to this
            # session, so it resets on the next one.
            #
            # Re-check eligibility server-side rather than trusting the
            # client's ``content.allow_all_edits`` flag alone: the flag is
            # only meaningful for the edit-tool / prompting-mode prompts the
            # affordance was offered for. Without this, a client could send
            # the flag on e.g. a Bash prompt and flip the session into
            # ``acceptEdits`` — a mode switch it was never offered.
            if (
                behavior == "allow"
                and isinstance(result.content, dict)
                and result.content.get("allow_all_edits") is True
                and _allow_all_edits_eligible(tool_name, permission_mode)
            ):
                decision["updatedPermissions"] = [
                    {
                        "type": "setMode",
                        # The plan card's "Yes, and use auto mode" switches the
                        # session into Claude's ``auto`` mode; the edit-tool
                        # "Accept & allow all edits" keeps the narrower
                        # ``acceptEdits`` (auto-approve edits only).
                        "mode": "auto" if tool_name == "ExitPlanMode" else "acceptEdits",
                        "destination": "session",
                    }
                ]
            elif behavior == "allow" and tool_name == "ExitPlanMode":
                # Plan approved WITHOUT auto mode — the card's "Yes,
                # manually approve edits". Pin the session to the prompting
                # ``default`` mode instead of trusting whatever mode
                # Claude's plan-exit restores, so every subsequent edit
                # prompts exactly as the button promised. De-escalation
                # only (most restrictive prompting mode), so no eligibility
                # gate is needed.
                decision["updatedPermissions"] = [
                    {"type": "setMode", "mode": "default", "destination": "session"}
                ]
            body = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                },
            }
            return Response(
                content=json.dumps(body),
                media_type="application/json",
            )

        # ── Proto event-type → internal Phase mapping ────────────────────
        _PROTO_EVENT_TYPE_TO_PHASE: dict[str, Phase] = {
            "PHASE_TOOL_CALL": Phase.TOOL_CALL,
            "PHASE_TOOL_RESULT": Phase.TOOL_RESULT,
            "PHASE_LLM_REQUEST": Phase.LLM_REQUEST,
            "PHASE_LLM_RESPONSE": Phase.LLM_RESPONSE,
            # A native session's UserPromptSubmit hook posts the request phase
            # here (the server-level _evaluate_input_policy skips native message
            # events). The prompt text rides in ``event.data.text``.
            "PHASE_REQUEST": Phase.REQUEST,
        }
        _PHASE_TO_PROTO_ACTION: dict[PolicyAction, str] = {
            PolicyAction.ALLOW: "POLICY_ACTION_ALLOW",
            PolicyAction.DENY: "POLICY_ACTION_DENY",
            PolicyAction.ASK: "POLICY_ACTION_ASK",
        }

