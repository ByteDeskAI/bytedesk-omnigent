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

def _tool_interceptors() -> dict[str, Callable[..., object]]:
    """The aggregated ``{prefix: handler}`` interceptor table (memoized).

    Deferred import of :mod:`omnigent.kernel.extensions` keeps the FastAPI/extension
    stack off this module's import path. When no extension contributes an
    interceptor the table is empty and every tool falls through to runner
    dispatch (back-compatible default).
    """
    global _TOOL_INTERCEPTORS_CACHE
    if _TOOL_INTERCEPTORS_CACHE is None:
        from omnigent.kernel.extensions import extension_tool_interceptors

        _TOOL_INTERCEPTORS_CACHE = extension_tool_interceptors()
    return _TOOL_INTERCEPTORS_CACHE

def _mcp_ok_response(rpc_id: int | str | None, result: dict[str, Any]) -> Response:
    """
    Wrap *result* in a JSON-RPC 2.0 success response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param result: The JSON-serialisable result payload, e.g.
        ``{"tools": [...]}``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return Response(content=body, media_type="application/json")

def _mcp_error_response(
    rpc_id: int | str | None,
    code: int,
    message: str,
) -> Response:
    """
    Wrap an error in a JSON-RPC 2.0 error response.

    :param rpc_id: The JSON-RPC request id. Use ``None`` when the id
        could not be parsed, e.g. ``None``.
    :param code: JSON-RPC error code, e.g. ``-32601`` (method not found)
        or ``-32000`` (application error).
    :param message: Human-readable error description,
        e.g. ``"Method not found: 'unsupported/method'"``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 error envelope.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
    )
    return Response(content=body, media_type="application/json")

def _mcp_input_required_response(
    rpc_id: int | str | None,
    elicitation_id: str,
    message: str,
    request_state: str,
    session_id: str | None = None,
) -> Response:
    """
    Return an MCP ``InputRequiredResult`` asking the runner to collect
    user approval before retrying the tool call.

    Follows the Multi Round-Trip Requests (MRTR) spec:
    ``https://modelcontextprotocol.io/specification/draft/basic/utilities/mrtr``.
    The ``elicitation_id`` is used as the key in ``inputRequests`` so the
    runner can identify the approval Future without inspecting the opaque
    ``requestState``. When URL-mode is active and ``session_id`` is
    known, adds ``mode``/``url`` to params.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param elicitation_id: Server-minted elicitation id used both as the
        ``inputRequests`` key and inside the opaque ``requestState``,
        e.g. ``"elicit_abc123"``.
    :param message: Human-readable prompt shown to the user,
        e.g. ``"Allow tool sys_os_shell?"``.
    :param request_state: Opaque state blob the client echoes on retry.
        Contains the ``elicitation_id`` and ``session_id`` so the server
        can verify authenticity on retry without server-side storage.
    :param session_id: Session/conversation id for constructing the
        approval page URL, e.g. ``"conv_abc123"``. ``None`` omits the
        URL (form mode).
    :returns: A :class:`Response` carrying the JSON-RPC 2.0
        ``InputRequiredResult`` envelope.
    """

    params: dict[str, Any] = {
        "message": message,
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    }
    if session_id is not None and _ELICITATION_MODE == "url":
        params["mode"] = "url"
        params["url"] = f"/approve/{session_id}/{elicitation_id}"
    else:
        params["mode"] = "form"

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": params,
                    }
                },
                "requestState": request_state,
            },
        }
    )
    return Response(content=body, media_type="application/json")

async def _handle_mcp_tools_list(
    rpc_id: int | str | None,
    session_id: str,
    runner_router: RunnerRouter | None,
) -> Response:
    """
    Handle a ``tools/list`` JSON-RPC request for the MCP proxy endpoint.

    Delegates execution to the runner's ``POST
    /v1/sessions/{id}/mcp/execute`` endpoint so that stdio MCP
    subprocesses spawn on the runner's machine (correct ``cwd``,
    env, and tooling). The Omnigent server's role here is routing only —
    policy evaluation happens in ``tools/call``.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id whose agent's tools to list,
        e.g. ``"conv_abc123"``.
    :param runner_router: Router used to get an httpx client pointed
        at the session's runner. ``None`` returns an error.
    :returns: A JSON-RPC 2.0 ``tools/list`` result response, or an
        error response when the runner is unavailable.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    _logger.debug("MCP tools/list: delegating to runner execute for session=%r", session_id)
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={"method": "tools/list", "params": {}},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in data:
        err = data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    result = data.get("result", {})
    # schemas are already in OpenAI function-tool format from RunnerMcpManager;
    # convert back to MCP inputSchema format for the tools/list response since
    # ProxyMcpManager on the runner expects MCP-shaped tools/list output.
    schemas: list[dict[str, Any]] = result.get("schemas", [])
    tools = []
    for schema in schemas:
        # schema shape: {"type": "function", "name": "srv__tool",
        #                "description": "...", "parameters": {...}}
        tools.append(
            {
                "name": schema.get("name", ""),
                "description": schema.get("description", ""),
                "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    failures: dict[str, str] = result.get("failures", {})
    for srv, msg in failures.items():
        _logger.warning("runner MCP server %r unavailable: %s", srv, msg)

    _logger.debug(
        "MCP tools/list: session=%r returning %d tools, %d failures",
        session_id,
        len(tools),
        len(failures),
    )
    return _mcp_ok_response(rpc_id, {"tools": tools})

def _mint_acting_identity_header(
    request: Request | None,
    auth_provider: AuthProvider | None,
    agent_id: str | None,
    session_id: str | None = None,
) -> dict[str, str] | None:
    """BDP-2424 P2 — mint the signed ``X-Omnigent-Acting-Identity`` header for a
    runner dispatch, or ``None`` (⇒ no header ⇒ today's behaviour).

    Degrades to ``None`` when there is no request, no auth provider, no inbound
    principal, or no configured signer — the same degrade-to-default rule the
    runner-side decode (Path A) relies on. ``get_principal`` is the canonical
    auth-chain Adapter (BDP-2388 / ADR-0149).

    BDP-2434: folds the per-session ``subject_token`` Office stashed on the
    inbound ``create_session`` / ``post_event`` routes (the ``tools/call`` hop
    that mints this carrier does NOT carry ``X-Bytedesk-Subject-Token``), so a
    ByteDesk.Mcp egress can present an on-behalf-of bearer. Absent ⇒ unchanged.
    """
    if request is None or auth_provider is None:
        return None
    principal = auth_provider.get_principal(request)
    if principal is None:
        return None
    signer = getattr(request.app.state, "assertion_signer", None)
    if signer is None:
        return None
    from omnigent.identity.identity import ActingIdentity
    from omnigent.identity.signer import HEADER_NAME, encode_acting_identity
    from omnigent.server.subject_token_stash import get_subject_token

    subject_token = (
        get_subject_token(request.app.state, session_id) if session_id is not None else None
    )
    token = encode_acting_identity(
        ActingIdentity(principal=principal, agent_id=agent_id, subject_token=subject_token),
        signer,
    )
    return {HEADER_NAME: token} if token else None

async def _handle_mcp_tools_call(
    rpc_id: int | str | None,
    session_id: str,
    params: dict[str, Any],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    request: Request | None = None,
    auth_provider: AuthProvider | None = None,
) -> Response:
    """
    Handle a ``tools/call`` JSON-RPC request for the MCP proxy endpoint.

    Steps:

    1. Validate the tool name (namespaced like ``github__search`` for MCP
       tools, or bare like ``sys_os_read`` for runner-local tools).
    2. Load session → agent → spec for policy evaluation.
    3. On first call: evaluate TOOL_CALL policy.  On DENY, return error.
       On ASK, emit a ``response.elicitation_request`` SSE event and
       return an MCP ``InputRequiredResult`` so the runner can park for
       user approval and retry per the MRTR spec.
    4. On retry (``requestState`` present in ``params``): verify the
       state, check the user's ``inputResponses``, and proceed if
       approved.
    5. Delegate execution to the runner's ``POST
       /v1/sessions/{id}/mcp/execute`` endpoint via the WS tunnel so
       that stdio MCP subprocesses and runner-local tools execute on the
       runner's machine (correct ``cwd``, environment, and tooling).
    6. Evaluate the TOOL_RESULT policy phase on the returned output;
       replace with a redaction notice on DENY.
    7. Return the result in MCP ``content`` format.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id, e.g. ``"conv_abc123"``.
    :param params: The JSON-RPC ``params`` object.  On first call,
        contains ``"name"`` and ``"arguments"``.  On retry, also
        contains ``"requestState"`` (opaque blob from the server) and
        ``"inputResponses"`` (user's approval decision), e.g.
        ``{"name": "sys_os_shell", "arguments": {}, "requestState": "...",
        "inputResponses": {"elicit_abc": {"action": "accept"}}}``.
    :param conversation_store: Store for session and label state.
    :param agent_store: Store for agent lookup.
    :param runner_router: Router used to get a tunneled client pointed at
        the session's runner. ``None`` returns an error response.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: A JSON-RPC 2.0 response carrying the tool result as MCP
        ``content`` blocks, an ``InputRequiredResult`` on ASK, or an
        error response when the call is denied, the runner is
        unavailable, or the underlying MCP call fails.
    """

    namespaced_name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments") or {}
    request_state_str: str | None = params.get("requestState")
    input_responses: dict[str, Any] = params.get("inputResponses") or {}
    is_retry = request_state_str is not None

    _logger.debug(
        "MCP tools/call: session=%r tool=%r is_retry=%r",
        session_id,
        namespaced_name,
        is_retry,
    )

    if not namespaced_name:
        return _mcp_error_response(rpc_id, -32000, "Missing tool name in tools/call params")

    # Session → agent → spec (needed for policy evaluation on both paths).
    # All three reads — conversation row, agent row, and the cold-cache
    # bundle fetch + spec parse — are blocking IO. Run them off the event
    # loop so an MCP tool call doesn't stall the single-worker server and
    # serialize concurrent requests behind it.
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.agent_id is None:
        return _mcp_error_response(
            rpc_id, -32000, f"Session not found or has no agent: {session_id!r}"
        )

    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return _mcp_error_response(rpc_id, -32000, f"Agent not found: {conv.agent_id!r}")

    # BDP-2424 P2: mint the per-agent acting-identity carrier once (degrades to
    # ``None`` ⇒ unchanged) and attach it on every runner dispatch below.
    acting_headers = _mint_acting_identity_header(
        request, auth_provider, conv.agent_id, session_id=session_id
    )

    # Build the policy engine once — used for both TOOL_CALL (first call
    # only) and TOOL_RESULT (both paths). Engine construction reads
    # session-policy specs and labels from the DB, so keep it off-loop too.
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )

    if is_retry:
        # ── Retry path: user has responded to the elicitation ────────
        # Verify the opaque requestState.
        try:
            state = json.loads(request_state_str)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return _mcp_error_response(rpc_id, -32000, "Invalid requestState: not valid JSON")
        if state.get("session_id") != session_id:
            # Reject cross-session replay.
            return _mcp_error_response(rpc_id, -32000, "requestState session mismatch")

        # ── Fail-closed: re-evaluate TOOL_CALL policy on retry ──────
        # The original retry path trusted the caller-supplied
        # requestState + inputResponses as proof that "policy ran and
        # the user approved." Because requestState is unsigned JSON
        # and inputResponses is caller-controlled, a forged retry
        # could bypass DENY/ASK gates entirely. Re-evaluating the
        # policy on every retry closes this vector: a DENY'd tool
        # stays denied regardless of what the request body claims.
        retry_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        retry_result = await engine.evaluate(retry_ctx)

        _logger.debug(
            "MCP tools/call retry TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            retry_result.action,
            retry_result.reason,
        )

        if retry_result.action == PolicyAction.DENY:
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {retry_result.reason or 'no reason given'}",
            )

        if retry_result.action == PolicyAction.ASK:
            # Policy still requires approval — verify the elicitation
            # was genuinely issued by the server (present in the
            # server-side pending map) and that the user approved it.
            elicitation_id_from_state: str = state.get("elicitation_id", "")
            if elicitation_id_from_state not in _pending_policy_ask_writes:
                # The elicitation_id is not in the server-side map.
                # Either it was forged, already consumed, or expired.
                # Check inputResponses: if the caller claims approval
                # for an unrecognised elicitation, reject it.
                approval: dict[str, Any] = input_responses.get(elicitation_id_from_state) or {}
                if approval.get("action") == "accept":
                    # Claimed approval for an elicitation the server
                    # never issued or already consumed — reject.
                    return _mcp_error_response(
                        rpc_id,
                        -32000,
                        "Elicitation not found or already resolved",
                    )
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            approval = input_responses.get(elicitation_id_from_state) or {}
            if approval.get("action") != "accept":
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            # Recover any policy-transformed args that were serialised into
            # requestState on the initial ASK — the client re-sends the
            # original arguments which we must not use when a transform was set.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
            # Apply the deciding policy's deferred writes now that the
            # user approved (POLICIES.md §7.2: only on accept).
            _pending = _pending_policy_ask_writes.pop(elicitation_id_from_state, None)
            if _pending is not None:
                if _pending.set_labels:
                    await asyncio.to_thread(engine.apply_label_writes, _pending.set_labels)
                if _pending.state_updates:
                    await asyncio.to_thread(engine.apply_state_updates, _pending.state_updates)
        else:
            # ALLOW — policy no longer requires approval (e.g. label
            # state changed between the original ASK and this retry).
            # Recover transformed args if present, then fall through.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
        # Fall through to execution.
    else:
        # ── First call: evaluate TOOL_CALL policy ────────────────────
        call_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        call_result = await engine.evaluate(call_ctx)

        _logger.debug(
            "MCP tools/call TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            call_result.action,
            call_result.reason,
        )

        if call_result.action == PolicyAction.DENY:
            if call_result.set_labels:
                await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {call_result.reason or 'no reason given'}",
            )

        if call_result.action == PolicyAction.ASK:
            # Emit elicitation SSE event (for REPL approval UI) and return
            # InputRequiredResult per the MCP MRTR spec so the runner can
            # park on the approval Future and retry when the user decides.
            elicitation_id = await _register_policy_elicitation(
                session_id,
                call_result,
                json.dumps(arguments)[:1024],
                conversation_store,
            )
            # Defer the deciding policy's writes (label mutations AND
            # state_updates such as a cost-budget checkpoint) to the
            # approved retry path — POLICIES.md §7.2 lands them only on
            # accept. The approval handler at the top of this function
            # already applies both via ``apply_label_writes`` and
            # ``apply_state_updates``. Mirrors the relay path pattern.
            # Always store an entry even when there are no deferred
            # writes — the retry path checks the pending map to verify
            # the elicitation was genuinely issued by the server. A
            # missing entry causes "Elicitation not found or already
            # resolved" on the retry.
            _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
                state_updates=call_result.state_updates,
                set_labels=call_result.set_labels,
                from_mcp=True,
            )
            request_state_payload: dict[str, Any] = {
                "elicitation_id": elicitation_id,
                "session_id": session_id,
            }
            # If the policy returned transformed args alongside ASK (e.g.
            # PII-redacted arguments), persist them so the retry path can
            # apply them after the user approves — the client re-sends the
            # original arguments, which would silently bypass the transform.
            if call_result.data is not None:
                request_state_payload["transformed_arguments"] = call_result.data
            request_state = json.dumps(request_state_payload)
            return _mcp_input_required_response(
                rpc_id,
                elicitation_id=elicitation_id,
                message=call_result.reason or "Approval required to run this tool",
                request_state=request_state,
                session_id=session_id,
            )
        # ALLOW — apply labels now that we know the action is not ASK.
        if call_result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
        # If the policy returned transformed arguments (e.g.
        # PII-redacted args), use them instead of the originals.
        if call_result.data is not None:
            arguments = call_result.data

    # ── BDP-2505: extension tool interception (was BDP-2458 server-side memory) ──
    # Some tools execute on the omnigent server itself rather than the runner —
    # e.g. the three-tier keyed memory__* tools, which the server owns alongside
    # the verified caller identity (conv.agent_id + the bundle's department). The
    # shared stdio memory front is a single subprocess with no per-call identity
    # channel, so it cannot carry a trustworthy per-agent identity (the BDP-2458
    # blocker); handling such tools here stamps the owner from the verified
    # identity (anti-spoof). Dispatch goes through the generic extension
    # ``tool_interceptors()`` prefix table (``_intercept_tool``) so core no longer
    # hard-imports ``bytedesk_omnigent.memory_tool_intercept`` by name — the
    # bytedesk extension claims the ``memory__`` prefix (ADR-0143 §5 Step 1). A
    # ``None`` return means no extension handled the tool → fall through to runner
    # dispatch. TOOL_CALL policy already ran above; TOOL_RESULT runs below,
    # identical to a runner-dispatched tool.
    _caller_dept = (spec.params or {}).get("department")
    _caller_dept = str(_caller_dept) if _caller_dept else None
    output = await asyncio.to_thread(
        _intercept_tool,
        namespaced_name,
        arguments,
        caller_agent_id=conv.agent_id,
        caller_department=_caller_dept,
    )

    if output is not None:
        result_ctx = EvaluationContext(
            phase=Phase.TOOL_RESULT,
            content={"result": output},
            tool_name=namespaced_name,
            request_data={"name": namespaced_name, "arguments": arguments},
            actor=actor,
        )
        result_policy = await engine.evaluate(result_ctx)
        if result_policy.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result_policy.set_labels)
        if result_policy.action == PolicyAction.DENY:
            output = f"[Result suppressed by policy: {result_policy.reason or 'no reason given'}]"
        elif isinstance(result_policy.data, str):
            output = result_policy.data
        return _mcp_ok_response(rpc_id, {"content": [{"type": "text", "text": output}]})

    # ── Execute on the runner via WS tunnel ──────────────────────────
    # The runner owns stdio subprocess spawning (correct machine, cwd,
    # and env). We call its /mcp/execute endpoint through the same WS
    # tunnel the runner already opened to the Omnigent server at startup.
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    try:
        from omnigent.runner.tool_dispatch import MCP_PROXY_FORWARD_TIMEOUT_S

        exec_resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": namespaced_name, "arguments": arguments},
            },
            headers=acting_headers,
            # ``sys_session_send`` returns a launch handle immediately; this
            # timeout now protects ordinary runner proxy hangs.
            timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
        )
        exec_resp.raise_for_status()
        exec_data = exec_resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in exec_data:
        err = exec_data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    # ── MRTR: external MCP server needs user input ───────────────
    # The runner returns ``{"result": {"input_required": {...}}}``
    # when the external MCP server sent an ``InputRequiredResult``.
    # Surface each elicitation to the user via the existing SSE
    # infrastructure, gather responses, then retry on the runner.
    mcp_input_required = exec_data.get("result", {}).get("input_required")
    if mcp_input_required is not None:
        if request is None:
            return _mcp_error_response(
                rpc_id, -32000, "MCP server requires elicitation but no request context available"
            )
        input_requests: dict[str, Any] = mcp_input_required.get("inputRequests") or {}
        mcp_request_state: str = mcp_input_required.get("requestState", "")

        # Gather user responses for each inputRequest.
        input_responses: dict[str, Any] = {}
        for eid, req_entry in input_requests.items():
            req_params = req_entry.get("params", {}) if isinstance(req_entry, dict) else {}
            elicit_params = ElicitationRequestParams(
                mode=req_params.get("mode", "form"),
                message=req_params.get("message", "Approval required"),
                requestedSchema=req_params.get("requestedSchema"),
            )
            elicit_result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=session_id,
                params=elicit_params,
                timeout_s=300.0,
                conversation_store=conversation_store,
            )
            if elicit_result is None:
                input_responses[eid] = {"action": "decline"}
            else:
                resp_entry: dict[str, Any] = {"action": elicit_result.action}
                if elicit_result.content is not None:
                    resp_entry["content"] = elicit_result.content
                input_responses[eid] = resp_entry

        # Retry on the runner with the user's inputResponses.
        try:
            retry_resp = await runner_client.post(
                f"/v1/sessions/{session_id}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {
                        "name": namespaced_name,
                        "arguments": arguments,
                        "inputResponses": input_responses,
                        "requestState": mcp_request_state,
                    },
                },
                headers=acting_headers,
                timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
            )
            retry_resp.raise_for_status()
            exec_data = retry_resp.json()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Runner MCP retry failed: %s", exc, exc_info=True)
            return _mcp_error_response(rpc_id, -32000, "Runner MCP retry failed.")
        if "error" in exec_data:
            err = exec_data["error"]
            return _mcp_error_response(
                rpc_id, err.get("code", -32000), err.get("message", "unknown error")
            )
        # Multi-round MRTR: the server returned yet another
        # InputRequiredResult on the retry. Return an error rather
        # than looping indefinitely — the user can retry the tool.
        if exec_data.get("result", {}).get("input_required") is not None:
            return _mcp_error_response(
                rpc_id,
                -32000,
                "MCP server requires additional elicitation rounds (not yet supported)",
            )

    output: str = exec_data.get("result", {}).get("output", "")
    _logger.debug(
        "MCP tools/call execute: session=%r tool=%r output_len=%d",
        session_id,
        namespaced_name,
        len(output),
    )

    # ── TOOL_RESULT policy ───────────────────────────────────────────
    result_ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": output},
        tool_name=namespaced_name,
        request_data={"name": namespaced_name, "arguments": arguments},
        actor=actor,
    )
    result_policy = await engine.evaluate(result_ctx)

    if result_policy.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result_policy.set_labels)

    _logger.debug(
        "MCP tools/call TOOL_RESULT policy: session=%r tool=%r action=%r reason=%r",
        session_id,
        namespaced_name,
        result_policy.action,
        result_policy.reason,
    )

    if result_policy.action == PolicyAction.DENY:
        output = f"[Result suppressed by policy: {result_policy.reason or 'no reason given'}]"
    elif result_policy.data is not None:
        # Policy returned transformed output (e.g. PII-redacted content).
        # The TOOL_RESULT phase contract requires data to be a str; coerce
        # and warn rather than dropping the result if a policy author returns
        # the wrong type (common mistake: returning the full content dict).
        if not isinstance(result_policy.data, str):
            _logger.warning(
                "TOOL_RESULT policy data must be str; got %s — coercing via str()",
                type(result_policy.data).__name__,
            )
        output = (
            result_policy.data if isinstance(result_policy.data, str) else str(result_policy.data)
        )

    return _mcp_ok_response(
        rpc_id,
        {"content": [{"type": "text", "text": output}]},
    )

