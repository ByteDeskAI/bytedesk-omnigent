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

_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S = 86400.0

_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S = 300.0

_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES = 1024

_HARNESS_ELICITATION_REPARK_GRACE_S = 10.0

_CLAUDE_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_claude_[0-9a-f]{32}$")

def _native_ask_gate_lock(conversation_id: str, deciding_policy: str) -> asyncio.Lock:
    """
    Return the lock serializing native ASK gates for one (session, policy).

    Concurrent native tool calls that all trip the same ASKing policy must
    prompt the human once, not once each. Callers hold the returned lock
    across the entire human-approval wait and re-evaluate the policy under it;
    the first approval records a checkpoint that collapses the siblings to
    ALLOW. Get-or-create is race-free because there is no ``await`` between the
    lookup and the insert (single event loop).

    :param conversation_id: Omnigent conversation id whose ASK gate is being
        serialized, e.g. ``"conv_abc123"``. Sub-agent native tool calls
        evaluate against the parent conversation id, so they share its lock.
    :param deciding_policy: Name of the policy that produced the ASK verdict,
        e.g. ``"session_cost_guard"``. Distinct policies get distinct locks so
        their approval prompts can surface concurrently.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        caller for the same ``(conversation_id, deciding_policy)`` pair.
    """
    key = (conversation_id, deciding_policy)
    lock = _native_ask_gate_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _native_ask_gate_locks[key] = lock
    return lock

def _structured_ask_user_question(
    tool_input: Any,
) -> dict[str, Any] | None:
    """
    Build a structured AskUserQuestion payload for the elicitation
    params extras.

    Claude's PermissionRequest payload includes the full tool_input
    when the gated tool is AskUserQuestion. Rather than relying on
    the (truncated) ``content_preview`` JSON-string, we extract the
    questions + options here and ship them as a typed structure the
    UI consumes directly.

    The returned shape is the same one the UI's
    :file:`@/lib/askUserQuestion.ts` produces from its preview
    parser — so the front-end can treat both sources uniformly.

    :param tool_input: The ``tool_input`` field from the
        PermissionRequest payload.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when
        the input doesn't carry a usable AskUserQuestion shape (no
        questions, malformed options, etc.) — caller falls back to
        the binary preview-only render.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[dict[str, Any]] = []
        for opt in options_raw:
            if isinstance(opt, dict):
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                # ``preview`` is an optional richer snippet some
                # Claude builds attach to an option (rendered as a
                # <pre> below the option list when selected). Ride
                # it through verbatim so the UI can surface it.
                preview = opt.get("preview")
                if isinstance(preview, str) and preview:
                    option["preview"] = preview
                options.append(option)
            elif isinstance(opt, str) and opt:
                options.append({"label": opt})
        if not options:
            continue
        question: dict[str, Any] = {
            "question": question_text,
            "options": options,
            "multiSelect": entry.get("multiSelect") is True,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}

async def _publish_and_wait_for_harness_elicitation(
    request: Request,
    *,
    session_id: str,
    params: ElicitationRequestParams,
    timeout_s: float,
    conversation_store: ConversationStore | None = None,
    elicitation_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> ElicitationResult | None:
    """
    Publish one harness-originated elicitation and wait for web verdict.

    Mirrors the ``omnigent claude`` permission hook contract: the
    hook parks a server-side Future, publishes the standard
    ``response.elicitation_request`` event, waits until the session
    ``approval`` event resolves the Future, and always publishes
    ``response.elicitation_resolved`` when the upstream wait ends.

    The wait ends on the first of three signals: (1) the web verdict
    Future (session ``approval`` event); (2) the terminal-resolved
    Event, set when a mirrored tool result for this gated tool proves
    the prompt was answered in the native TUI (see
    :func:`_signal_terminal_resolved_harness_elicitation`); or (3)
    upstream disconnect / ``timeout_s``. Only (1) yields a verdict;
    (2) and (3) return ``None`` (fail-ask). (1) and (2) publish
    ``response.elicitation_resolved`` immediately; (3) defers it by
    ``_HARNESS_ELICITATION_REPARK_GRACE_S`` and skips it when the
    caller re-parks the same ``elicitation_id`` (hook retries after a
    severed long-poll reuse their id), so a still-blocked prompt's
    card survives the gap. A caller-supplied id likewise re-attaches
    to a verdict that landed during a gap via the pre-resolved
    tombstone, returned at registration time without re-publishing.

    :param request: FastAPI request object so upstream disconnect can
        be detected.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param params: Elicitation params to publish.
    :param timeout_s: Maximum wait in seconds, e.g. ``300.0``.
    :param conversation_store: Optional store used to mirror
        child-session prompts into ancestor streams. ``None`` keeps
        the prompt scoped to ``session_id`` only.
    :param elicitation_id: Optional precomputed correlation id, e.g.
        ``"elicit_codex_abc123"``. ``None`` mints a random id.
    :param tool_name: Gated tool name, e.g. ``"Bash"``, used to
        correlate a mirrored tool result back to this prompt for the
        terminal-resolved fast path. ``None`` (e.g. Codex) disables
        that correlation; the prompt still resolves via web verdict,
        disconnect, or timeout.
    :param tool_input: Gated tool input, e.g. ``{"command": "ls"}``,
        used with ``tool_name`` to disambiguate the result when several
        same-named prompts are parked at once.
    :returns: Web verdict, or ``None`` on terminal-side resolution,
        timeout, or disconnect.
    """
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    # ``resolved_elsewhere`` is set when a native-side signal proves the
    # prompt was answered outside the web UI: either a mirrored tool
    # result for this gated tool, or Codex app-server's exact
    # ``serverRequest/resolved`` notification. Raced below so the wait
    # ends promptly without relying on the web verdict or on disconnect
    # detection (unreliable behind the Databricks Apps proxy).
    parked = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        resolved_elsewhere=asyncio.Event(),
    )
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = parked
    # settled = verdict / terminal-resolved (clear the card now); a
    # severed wait instead defers the clear so a hook retry can re-park.
    published_request = False
    settled = False
    try:
        tombstone = _consume_pre_resolved_harness_elicitation(session_id, elicitation_id)
        if tombstone is not None:
            # Verdict from the un-parked gap; None = terminal answered (fail-ask).
            return tombstone.result
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=params,
        )
        event_payload = event.model_dump()
        session_stream.publish(session_id, event_payload)
        published_request = True
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                event_payload,
            )
        disconnect_task = asyncio.create_task(
            _poll_request_disconnect(request),
        )
        resolved_elsewhere_task = asyncio.create_task(parked.resolved_elsewhere.wait())
        race_tasks = (disconnect_task, resolved_elsewhere_task)
        try:
            done, _pending = await asyncio.wait(
                {future, *race_tasks},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for race_task in race_tasks:
                if not race_task.done():
                    race_task.cancel()
                    # Bounded: a cancellation swallowed inside the race
                    # task (e.g. coalesced into an anyio cancel-scope
                    # unwind) must not convert this cleanup into
                    # an unbounded wait — that wedged the whole request
                    # for the gate's timeout. ``asyncio.wait`` absorbs
                    # the CancelledError outcome; an unreaped task is
                    # logged and abandoned to die with the request.
                    _reaped, still_pending = await asyncio.wait(
                        {race_task},
                        timeout=_RACE_TASK_REAP_TIMEOUT_S,
                    )
                    if still_pending:
                        _logger.warning(
                            "Race task %r for elicitation %s survived its "
                            "cancellation (swallowed cancel); abandoning it.",
                            race_task.get_coro(),
                            elicitation_id,
                        )
        # Only an actual web verdict yields a result; a terminal-side
        # resolution, disconnect, or timeout returns None (fail-ask).
        # Checking ``future in done`` (not ``future.done()``) avoids
        # honoring a verdict that lands in the same tick as a disconnect.
        if future in done and future.exception() is None:
            settled = True
            return future.result()
        settled = parked.resolved_elsewhere.is_set()
        return None
    finally:
        # Pop only our own entries — a hook retry may have re-parked
        # this id with a new future while this wait was unwinding.
        if _harness_elicitation_registry.get(elicitation_id) is future:
            _harness_elicitation_registry.pop(elicitation_id, None)
            _harness_elicitation_owners.pop(elicitation_id, None)
        if _harness_parked_elicitations.get(elicitation_id) is parked:
            _harness_parked_elicitations.pop(elicitation_id, None)
        if published_request and not settled:
            # Severed without an answer — defer the clear (scheduled
            # before any await so handler cancellation can't skip it).
            _schedule_deferred_elicitation_clear(
                session_id,
                elicitation_id,
                conversation_store,
            )
        elif published_request:
            _publish_elicitation_resolved(session_id, elicitation_id)
            if conversation_store is not None:
                await asyncio.to_thread(
                    _publish_elicitation_resolved_to_ancestors,
                    conversation_store,
                    session_id,
                    elicitation_id,
                )

def _signal_terminal_resolved_harness_elicitation(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
) -> None:
    """
    Resolve the parked prompt a mirrored tool result belongs to,
    ending its long-poll promptly.

    Called when the transcript forwarder mirrors a tool result
    (``function_call_output``) for a native session. A tool result is
    only written AFTER the user answered that tool's permission prompt
    in the native terminal — on accept the tool ran and produced output,
    on reject the harness records a rejection result — so its arrival is
    a reliable "the terminal already resolved this" signal.

    Correlation is by tool identity, never positional: a result only
    resolves a parked prompt for the SAME ``tool_name`` in the same
    session. That is what stops an unrelated auto-allowed tool's output
    from clearing a different tool's pending prompt. Among
    same-named parked prompts it prefers an exact ``tool_input`` match;
    if none match exactly but exactly one same-named prompt is parked it
    resolves that one (the hook payload and the transcript can serialize
    identical input differently, so a single unambiguous candidate is
    treated as the match). If several same-named prompts are parked and
    none match by input it stays conservative and resolves none — the
    web verdict or timeout still applies.

    Best-effort and idempotent: a no-op when no parked prompt matches
    (e.g. the web UI already resolved it, the tool needed no permission,
    or it is an unrelated tool). Harness-agnostic by construction —
    keyed on the parked prompt's tool identity, not on a claude-native
    check — so a Codex hook that records ``tool_name`` benefits too.

    :param session_id: Omnigent conversation id whose forwarder mirrored the
        result, e.g. ``"conv_abc123"``.
    :param tool_name: Tool name the result is for, e.g. ``"Bash"``.
    :param tool_input: Tool input the result is for, e.g.
        ``{"command": "ls"}``, or ``None`` if unavailable.
    """
    candidates = [
        parked
        for parked in _harness_parked_elicitations.values()
        if parked.session_id == session_id
        and parked.tool_name == tool_name
        and not parked.resolved_elsewhere.is_set()
    ]
    if not candidates:
        return
    for parked in candidates:
        if parked.tool_input == tool_input:
            parked.resolved_elsewhere.set()
            return
    if len(candidates) == 1:
        candidates[0].resolved_elsewhere.set()

def _schedule_deferred_elicitation_clear(
    session_id: str,
    elicitation_id: str,
    conversation_store: ConversationStore | None,
) -> None:
    """
    Clear one elicitation's approval card after the re-park grace, unless
    a hook retry re-parks the id first.

    A wait severed without an answer (proxy cut, timeout) may still be
    blocked in the native terminal; clearing immediately wiped the only
    surface a headless sub-agent's user can answer from. A hook that
    died for real never re-parks, so the clear still fires after the
    grace and badges don't stick.

    :param session_id: Session that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param elicitation_id: Correlation id whose card may need clearing,
        e.g. ``"elicit_claude_0f3a..."``.
    :param conversation_store: Store used to mirror the clear into
        ancestor streams, or ``None`` to keep it session-local.
    """

    async def _clear_after_grace() -> None:
        """
        Sleep out the grace, then publish the clear unless re-parked.

        :returns: None.
        """
        await asyncio.sleep(_HARNESS_ELICITATION_REPARK_GRACE_S)
        if elicitation_id in _harness_elicitation_registry:
            # Re-parked — the new wait owns the eventual clear.
            return
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )

    task = asyncio.create_task(_clear_after_grace())
    _deferred_elicitation_clear_tasks.add(task)
    task.add_done_callback(_deferred_elicitation_clear_tasks.discard)

def _client_supplied_hook_elicitation_id(
    payload: dict[str, Any],
    session_id: str,
) -> str | None:
    """
    Validate the hook client's optional re-attach elicitation id.

    The hook mints one stable id per prompt and re-sends it on every
    retry POST, so a severed wait re-parks as the SAME elicitation.
    Client-controlled, so it is constrained to the claude-hook
    namespace and may not collide with another session's parked id.

    :param payload: Parsed PermissionRequest hook body. Reads the
        optional ``_omnigent_elicitation_id`` key.
    :param session_id: Session the hook call is for, e.g.
        ``"conv_abc123"``.
    :returns: The validated id, or ``None`` when the client supplied
        none (the wait mints a random id as before).
    :raises OmnigentError: 400 when the id is malformed or is
        currently parked by a different session.
    """
    raw = payload.get("_omnigent_elicitation_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not _CLAUDE_HOOK_ELICITATION_ID_RE.fullmatch(raw):
        raise OmnigentError(
            "PermissionRequest hook '_omnigent_elicitation_id' must match "
            "'elicit_claude_' + 32 hex chars.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(raw)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation id belongs to a different session.",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw

def _consume_pre_resolved_harness_elicitation(
    session_id: str,
    elicitation_id: str,
) -> _PreResolvedHarnessElicitation | None:
    """
    Consume a resolution that arrived before the hook wait registered.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: The consumed tombstone when one matched this session
        (its ``result`` carries the web verdict to honor, or ``None``
        for a terminal-side resolution), or ``None`` when nothing was
        pre-resolved.
    """
    _prune_pre_resolved_harness_elicitations()
    tombstone = _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    if tombstone is None:
        return None
    if tombstone.session_id == session_id:
        return tombstone
    _harness_pre_resolved_elicitations[elicitation_id] = tombstone
    return None

def _prune_pre_resolved_harness_elicitations(now: float | None = None) -> None:
    """
    Prune stale or excess pre-resolved harness elicitation tombstones.

    :param now: Optional wall-clock timestamp from ``time.time()``,
        e.g. ``1710000000.0``. ``None`` reads the current time.
    :returns: None.
    """
    if not _harness_pre_resolved_elicitations:
        return
    now = time.time() if now is None else now
    expired = [
        elicitation_id
        for elicitation_id, tombstone in _harness_pre_resolved_elicitations.items()
        if now - tombstone.created_at > _HARNESS_PRE_RESOLVED_ELICITATION_TTL_S
    ]
    for elicitation_id in expired:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    overflow = (
        len(_harness_pre_resolved_elicitations) - _HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES
    )
    if overflow <= 0:
        return
    oldest = sorted(
        _harness_pre_resolved_elicitations.items(),
        key=lambda item: item[1].created_at,
    )[:overflow]
    for elicitation_id, _tombstone in oldest:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)

def _signal_harness_elicitation_resolved_by_id(
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Resolve or pre-resolve one parked harness elicitation by id.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: None.
    :raises OmnigentError: If the id is malformed or belongs to a
        different session.
    """
    if not elicitation_id:
        raise OmnigentError(
            "external_elicitation_resolved requires data.elicitation_id.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(elicitation_id)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation does not belong to this session.",
            code=ErrorCode.INVALID_INPUT,
        )
    _prune_pre_resolved_harness_elicitations()
    parked = _harness_parked_elicitations.get(elicitation_id)
    if parked is None:
        _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
            session_id=session_id,
            created_at=time.time(),
        )
        _prune_pre_resolved_harness_elicitations()
        return
    parked.resolved_elsewhere.set()

def _targeted_elicitation_event(
    event: dict[str, Any],
    *,
    target_session_id: str,
) -> dict[str, Any]:
    """
    Return an elicitation event annotated with its resolution target.

    Child-session elicitations can be mirrored into an ancestor's
    chat stream. The mirrored card is rendered in the ancestor
    conversation, but the harness Future still belongs to the child.
    ``target_session_id`` tells clients which session's resolve URL
    should receive the verdict.

    :param event: Original ``response.elicitation_request`` event,
        e.g. ``{"type": "response.elicitation_request",
        "elicitation_id": "elicit_abc", "params": {...}}``.
    :param target_session_id: Session that owns the parked
        elicitation, e.g. ``"conv_child123"``.
    :returns: A shallow event copy with a copied ``params`` dict
        carrying ``target_session_id``.
    """
    mirrored = dict(event)
    params = event.get("params")
    if isinstance(params, dict):
        mirrored["params"] = {**params, "target_session_id": target_session_id}
    else:
        mirrored["params"] = {"target_session_id": target_session_id}
    return mirrored

def _publish_elicitation_request_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """
    Mirror a child elicitation request into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param event: Original ``response.elicitation_request`` event.
    """
    mirrored = _targeted_elicitation_event(event, target_session_id=session_id)
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        session_stream.publish(ancestor_id, mirrored)

def _publish_elicitation_resolved_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Mirror an elicitation-resolved event into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param elicitation_id: Elicitation correlation id, e.g.
        ``"elicit_abc123"``.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        _publish_elicitation_resolved(ancestor_id, elicitation_id)

def _pending_elicitation_snapshot_for_session(
    conv_store: ConversationStore,
    conv: Conversation,
) -> list[dict[str, Any]]:
    """
    Return pending elicitation events visible from a session snapshot.

    The current session's own outstanding prompts are returned first.
    Pending prompts from descendant sub-agents are appended with
    ``params.target_session_id`` so a cold-loaded ancestor chat can
    render and resolve child approvals.
    Duplicate ids are skipped because live mirroring also records the
    ancestor copy in the in-memory index.

    The descendant walk costs one ``list_conversations`` query per
    session in the tree, so it is skipped entirely unless some session
    other than ``conv`` has an outstanding prompt in the in-memory
    index (the common case is none anywhere).

    :param conv_store: Store used to list descendant sub-agents.
    :param conv: Session conversation being snapshotted.
    :returns: Pending elicitation event dicts suitable for
        :class:`SessionResponse.pending_elicitations`.
    """
    events = pending_elicitations.snapshot_for(conv.id)
    if not (set(pending_elicitations.pending_session_ids()) - {conv.id}):
        return events
    seen = {
        event.get("elicitation_id")
        for event in events
        if isinstance(event.get("elicitation_id"), str)
    }
    for child in _descendant_sessions(conv_store, conv.id):
        for event in pending_elicitations.snapshot_for(child.id):
            elicitation_id = event.get("elicitation_id")
            if isinstance(elicitation_id, str) and elicitation_id in seen:
                continue
            if isinstance(elicitation_id, str):
                seen.add(elicitation_id)
            events.append(_targeted_elicitation_event(event, target_session_id=child.id))
    return events

def _validated_harness_override(value: str | None, agent: Agent) -> str | None:
    """
    Validate + canonicalize a session-create ``harness_override``.

    Mirrors the CLI's ``--harness`` rules (``_apply_harness_override_to_executor``
    in ``omnigent/chat.py``): the canonical name must be a known bundle
    harness, and the bound agent must be an ``executor.type: omnigent``
    spec — other executor types have no ``config.harness``, so an
    override there would be a silent no-op.

    :param value: The raw override from the request body, e.g. ``"pi"``
        or the ``"openai-agents-sdk"`` alias. ``None`` means no override.
    :param agent: The bound agent row (already fetched by the caller).
    :returns: The canonical harness id, or ``None`` when *value* is.
    :raises OmnigentError: ``invalid_input`` for an unknown harness, a
        non-omnigent executor type, or an unloadable agent bundle.
    """
    if value is None:
        return None
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.runtime import get_agent_cache
    from omnigent.spec._omnigent_compat import (
        OMNIGENT_EXECUTOR_TYPE,
        OMNIGENT_HARNESSES,
    )

    canonical = canonicalize_harness(value) or value
    if canonical not in OMNIGENT_HARNESSES:
        raise OmnigentError(
            f"invalid harness_override: must be one of "
            f"{sorted(OMNIGENT_HARNESSES)}, got {value!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError) as exc:
        raise OmnigentError(
            f"harness_override requires a loadable agent spec; "
            f"agent {agent.name!r} failed to load: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    executor_type = loaded.spec.executor.type
    if executor_type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"harness_override only applies to executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r} agents; agent {agent.name!r} "
            f"declares executor.type {executor_type!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return canonical

def _publish_elicitation_resolved(session_id: str, elicitation_id: str) -> None:
    """
    Universal "approval done" signal — single publish drives both
    sidebar (via :func:`pending_elicitations.record_publish` decrement)
    and the chat-side ``ApprovalCard`` flip on every live subscriber.
    Idempotent on duplicate emissions for the same id.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Correlation id, e.g. ``"elicit_abc123"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
        },
    )

async def _resolve_elicitation(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore | None = None,
) -> None:
    """
    Resolve one outstanding elicitation from an approval payload.

    Shared by the two entry points that deliver a verdict for a
    parked elicitation: the ``type == "approval"`` branch of
    ``POST /v1/sessions/{id}/events`` and the dedicated
    ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` URL
    endpoint (URL-based elicitation). Both converge here so
    resolution semantics — server-side harness Future, sidebar
    badge clear, and runner forward — stay identical regardless of
    how the verdict arrived.

    Three effects, in order:

    1. **Server-side harness Future.** Claude-native permission
       hooks (and any other server-parked elicitation) register a
       Future in ``_harness_elicitation_registry``. If one exists
       for this id, is unresolved, and is owned by *this* session
       (cross-user guard), set its result. An
       ownership mismatch silently skips resolution — the runner
       forward below still fires so a runner-side elicitation with
       the same id can reject it on its own terms.
    2. **Sidebar badge clear.** Publish
       ``response.elicitation_resolved`` so every subscribed client
       (other tabs, the REPL TUI) flips its ``ApprovalCard`` and the
       pending-elicitation badge decrements. Idempotent.
    3. **Runner forward.** Runner-side elicitations (policy
       approvals parked in the runner's ``_pending_approvals`` dict)
       resolve when the approval event reaches the runner's
       ``/events``. Forwarded as a canonical ``approval`` event.

    :param session_id: Session/conversation identifier that owns
        the elicitation, e.g. ``"conv_abc123"``.
    :param data: Approval payload carrying the ``elicitation_id``
        correlation key plus the MCP ``ElicitationResult`` fields
        (``action``, optional ``content``), e.g.
        ``{"elicitation_id": "elicit_abc", "action": "accept"}``.
    :param runner_router: Router used to resolve the session's bound
        runner for the forward, or ``None`` in in-process setups
        (the forward is skipped when no runner is bound).
    :param conversation_store: Optional store used to mirror the
        resolved signal into ancestor streams when ``session_id`` is
        a child session. ``None`` keeps the signal scoped locally.
    """
    # Empty-string default is intentional, NOT a fail-loud miss: the
    # resolve-URL caller always supplies the id (it comes from the URL
    # path), but the public ``approval`` event caller may post a
    # malformed body. A missing id degrades gracefully below (no Future
    # matches, no resolved event published) rather than 500-ing the
    # client — the runner forward still fires so the runner can reject.
    elicitation_id = data.get("elicitation_id", "")
    harness_future = _harness_elicitation_registry.get(elicitation_id)
    if harness_future is not None and not harness_future.done():
        # Only the session that owns this elicitation
        # may resolve its server-side Future. A mismatch skips
        # resolution (the runner forward still fires below).
        if _harness_elicitation_owners.get(elicitation_id) == session_id:
            result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
            try:
                harness_future.set_result(
                    ElicitationResult.model_validate(result_payload),
                )
            except ValidationError:
                _logger.warning(
                    "Invalid approval payload for %r",
                    elicitation_id,
                    exc_info=True,
                )
    elif harness_future is None and isinstance(elicitation_id, str) and elicitation_id:
        # Nothing parked (severed long-poll mid-retry, or a runner-side
        # id that just ages out) — tombstone the verdict so a re-park
        # returns it; consume is session-checked, so no cross-session use.
        result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
        try:
            pre_resolved = ElicitationResult.model_validate(result_payload)
        except ValidationError:
            pre_resolved = None
        if pre_resolved is not None:
            _prune_pre_resolved_harness_elicitations()
            _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
                session_id=session_id,
                created_at=time.time(),
                result=pre_resolved,
            )
            _prune_pre_resolved_harness_elicitations()
    # Fan-out for every other subscribed client (other tabs, REPL
    # TUI). Idempotent vs. the runner's own ``wait_for_user_approval``
    # finally / harness hook finally — those also publish for the id.
    if isinstance(elicitation_id, str) and elicitation_id:
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )
    # Runner-side elicitations (policy approvals, scaffold dispatch)
    # resolve when the canonical approval event reaches the runner.
    await _forward_approval_to_runner(session_id, data, runner_router)

async def _hold_native_ask_gate(
    request: Request,
    *,
    session_id: str,
    phase: Phase,
    data: dict[str, Any],
    engine: PolicyEngine,
    result: PolicyResult,
    conversation_store: ConversationStore,
) -> bool:
    """
    Hold a server-side ASK gate until a human resolves it.

    Publishes a ``response.elicitation_request`` (the web UI / REPL
    render the approve card) and parks a server-side Future via
    :func:`_publish_and_wait_for_harness_elicitation`, exactly as the
    ``PermissionRequest`` hook does. The human approves through the
    elicitation's resolve URL; this collapses the verdict to a single
    boolean the caller maps to ALLOW / DENY.

    Used for any phase whose ASK must be resolved on the server rather
    than by a runner-side ``wait_for_user_approval`` park:
    :attr:`Phase.TOOL_CALL` (the native ``PreToolUse`` hook gate) and
    :attr:`Phase.REQUEST` (the user-message input gate, which has no
    runner in the loop yet — see :func:`_evaluate_input_policy`).

    Unlike the old ASK→``defer`` path, the gate lives on the server,
    so a permissive native ``permission_mode`` (``acceptEdits`` /
    ``bypassPermissions``) cannot skip it — the action stays blocked
    until a real human verdict. Timeout / disconnect fail closed
    (return ``False`` → DENY).

    On approve, the ASK-accumulated ``set_labels`` / ``state_updates``
    are applied (POLICIES.md §7.2: side effects land only on approve);
    a denied / timed-out ASK leaves no trace.

    :param request: FastAPI request, for upstream-disconnect detection
        inside the parking helper.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param phase: Enforcement phase being gated, e.g.
        :attr:`Phase.TOOL_CALL` or :attr:`Phase.REQUEST`.
    :param data: The proto event ``data`` — for a tool call,
        ``{"name": "Bash", "arguments": {"command": "ls"}}``; for a
        request, the user ``message`` body
        (``{"role": "user", "content": [...]}``).
    :param engine: The policy engine, used to resolve the per-policy
        ``ask_timeout`` and to apply approved side effects.
    :param result: The composed ASK :class:`PolicyResult` — carries
        the reason, deciding_policy, and withheld set_labels.
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :returns: ``True`` iff a human accepted; ``False`` on decline /
        cancel / timeout / disconnect (fail closed).
    """
    tool_name = data.get("name")
    tool_input = data.get("arguments")
    params = ElicitationRequestParams(
        mode="form",
        message=result.reason or "Approval required",
        requestedSchema={},
        phase=phase.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=json.dumps(data)[:1024],
    )
    # Per-policy ``ask_timeout`` override wins over the spec-level default.
    timeout_s = float(resolve_ask_timeout(engine, result))
    # Mint the id up front so we can also surface this ASK in the native
    # terminal (a tmux popup) before parking on the web verdict; both
    # surfaces resolve the same id, so whichever answers first releases the
    # gate.
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    _spawn_native_approval_popup_forward(
        session_id, elicitation_id, params.message, result.deciding_policy
    )
    verdict = await _publish_and_wait_for_harness_elicitation(
        request,
        session_id=session_id,
        params=params,
        timeout_s=timeout_s,
        elicitation_id=elicitation_id,
        conversation_store=conversation_store,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
    )
    approved = verdict is not None and verdict.action == "accept"
    if approved:
        # POLICIES.md §7.2: writes accumulated by the ASKing policy
        # land only on approve.
        if result.set_labels:
            engine.apply_label_writes(result.set_labels)
        if result.state_updates:
            engine.apply_state_updates(result.state_updates)
    return approved

def _drive_terminal_resolved_elicitation(session_id: str, persisted: ConversationItem) -> None:
    """
    Feed a mirrored tool item into the terminal-resolved fast path.

    A ``function_call`` records its tool identity by ``call_id`` so the
    matching ``function_call_output`` can be correlated back to a parked
    permission prompt. A ``function_call_output`` means the gated tool
    already ran (or was rejected) in the native terminal, so the prompt
    the web UI may still be showing was resolved there — resolve the
    matching parked prompt now instead of waiting for the hook timeout.
    Other item types are ignored.

    :param session_id: Omnigent conversation id the item was mirrored for,
        e.g. ``"conv_abc123"``.
    :param persisted: The stored conversation item the forwarder just
        mirrored via ``external_conversation_item``.
    """
    data = persisted.data
    if persisted.type == "function_call" and isinstance(data, FunctionCallData):
        try:
            parsed = json.loads(data.arguments) if data.arguments else {}
        except json.JSONDecodeError:
            parsed = {}
        _recent_mirrored_tool_calls[data.call_id] = _MirroredToolCall(
            tool_name=data.name,
            tool_input=parsed if isinstance(parsed, dict) else {},
            response_id=persisted.response_id,
        )
    elif persisted.type == "function_call_output" and isinstance(data, FunctionCallOutputData):
        identity = _recent_mirrored_tool_calls.get(data.call_id)
        if identity is not None:
            _signal_terminal_resolved_harness_elicitation(
                session_id, identity.tool_name, identity.tool_input
            )

async def _register_policy_elicitation(
    session_id: str,
    result: PolicyResult,
    arguments_preview: str,
    conversation_store: ConversationStore,
) -> str:
    """
    Publish an elicitation request event on the session stream.

    Approval state lives on the runner (in-memory
    ``_pending_approvals`` dict). The server just publishes the
    ``response.elicitation_request`` SSE event so the client
    sees the approval prompt, and returns the elicitation_id
    so the runner can key its Future on it.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param result: The :class:`PolicyResult` with action=ASK,
        carrying the reason and deciding_policy fields.
    :param arguments_preview: Truncated argument string for
        the elicitation UI preview (max ~1024 chars).
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :returns: The generated elicitation id,
        e.g. ``"elicit_a1b2c3..."``.
    """
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    elicitation = ElicitationRequest(
        message=result.reason or "Approval required",
        requested_schema={},
        phase=Phase.TOOL_CALL.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=arguments_preview[:1024],
    )
    # Approval state lives on the runner (in-memory
    # _pending_approvals dict of elicitation_id → Future).
    # The server just publishes the elicitation SSE event and
    # returns the elicitation_id. The runner parks on the
    # Future; the client's approval event is forwarded to the
    # runner which resolves it. No server-side state needed.
    _elicit_event = build_elicitation_request_event(
        elicitation_id, elicitation, session_id=session_id
    )
    session_stream.publish(session_id, _elicit_event)
    await asyncio.to_thread(
        _publish_elicitation_request_to_ancestors,
        conversation_store,
        session_id,
        _elicit_event,
    )
    return elicitation_id

