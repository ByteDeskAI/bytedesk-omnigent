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

def _build_session_response(
    conv: Conversation,
    items: list[ConversationItem],
    status: Literal["idle", "running", "failed"],
    permission_level: int | None = None,
    llm_model: str | None = None,
    context_window: int | None = None,
    last_total_tokens: int | None = None,
    last_task_error: dict[str, str] | None = None,
    agent_name: str | None = None,
    skills: list[SkillSummary] | None = None,
    runner_online: bool | None = None,
    host_online: bool | None = None,
    pending_elicitation_events: list[dict[str, Any]] | None = None,
    subtree_usage: dict[str, Any] | None = None,
) -> SessionResponse:
    """
    Build a :class:`SessionResponse` from store-side entities.

    ``status`` is derived from the conversation's tasks by the
    caller via :func:`_derive_session_lifecycle` — the conversation
    row itself owns no lifecycle column.

    :param conv: The persisted conversation entity.
    :param items: Committed conversation items in chronological
        order, each a :class:`ConversationItem`.
    :param status: Derived session lifecycle status,
        e.g. ``"running"``.
    :param permission_level: The requesting user's numeric level
        on this session (1=read, 2=edit, 3=manage), or ``None``
        when permissions are disabled.
    :param runner_online: Session-scoped liveness for the bound
        runner/host, e.g. ``False`` for a dead tunneled runner.
        ``None`` when no lookup is wired.
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when not available.
    :param context_window: Context window size in tokens looked up
        from litellm server-side, e.g. ``200_000``. ``None`` when
        the model is not in litellm's registry.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's usage, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param last_task_error: Error dict from the most recently failed
        task, e.g. ``{"code": "executor_error", "message": "..."}``.
        ``None`` when ``status`` is not ``"failed"`` or the task has
        no stored error.
    :param agent_name: Human-readable agent name, e.g.
        ``"research-agent"``. ``None`` when the agent row is not
        available at snapshot-build time.
    :param skills: Merged skill summaries (bundled + host) for
        the bound agent. ``None`` is treated as the empty list,
        e.g. when the agent spec cannot be loaded.
    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session (see
        :class:`SessionLiveness`). ``None`` when the caller has no
        liveness lookup wired (e.g. focused tests), in which case the
        field is omitted from the API projection.
    :param host_online: Whether the session's host tunnel is live, or
        ``None`` when the session has no ``host_id`` or no lookup is
        wired (see :class:`SessionLiveness`). Used only to decide what
        the open view shows when ``runner_online`` is ``False``.
    :param pending_elicitation_events: Optional precomputed
        outstanding elicitation events. ``None`` reads only the
        current session's entries from the pending-elicitations index.
    :param subtree_usage: Precomputed subtree usage dict (this session
        plus its sub-agent descendants, from
        :func:`load_session_usage`), used to display a cost that
        includes sub-agents, e.g. ``{"total_cost_usd": 11.19}``.
        ``None`` falls back to this conversation's own ``session_usage``
        (correct for childless sessions). Passed by the snapshot path;
        other callers omit it.
    :returns: The :class:`SessionResponse` for the API.
    :raises OmnigentError: If ``conv.agent_id`` is ``None``.
    """
    if conv.agent_id is None:
        raise OmnigentError(
            "Session has no agent binding",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Usage to display for this node: the SUBTREE total (this session + its
    # sub-agents) when the caller computed it, else this conversation's own
    # usage. Shared by the cost indicator and the per-model breakdown so
    # both read the same numbers.
    display_usage = subtree_usage if subtree_usage is not None else (conv.session_usage or {})
    return SessionResponse(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_name,
        status=status,
        created_at=conv.created_at,
        title=title_without_closed_marker(conv.title),
        labels=labels_with_closed_status(conv.labels, conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        tenant_id=conv.tenant_id,
        external_key=conv.external_key,
        runner_online=runner_online,
        host_online=host_online,
        reasoning_effort=conv.reasoning_effort,
        items=items,
        permission_level=permission_level,
        sub_agent_name=conv.sub_agent_name,
        parent_session_id=conv.parent_conversation_id,
        root_conversation_id=conv.root_conversation_id,
        llm_model=llm_model,
        harness=_resolve_harness(conv),
        model_override=conv.model_override,
        cost_control_mode_override=conv.cost_control_mode_override,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        # Seed the client's cost indicator on resume. Uses the SUBTREE
        # total (this session + its sub-agents) when the caller computed
        # it, so a parent's badge reflects its sub-agents' spend; falls
        # back to this conversation's own usage otherwise. A priced
        # cumulative total, or None (rendered "—") when never priced.
        total_cost_usd=_priced_cost_for_display(display_usage),
        # Per-model breakdown over the same subtree usage. None (omitted)
        # when no per-model usage was recorded.
        usage_by_model=_usage_by_model_for_display(display_usage),
        last_task_error=last_task_error,
        external_session_id=conv.external_session_id,
        terminal_launch_args=conv.terminal_launch_args,
        # Replay outstanding approval prompts into the snapshot.
        # The live SSE stream has no buffer, so a prompt emitted
        # before the user opened this chat would otherwise never
        # render — the UI rebuilds blocks from the snapshot on
        # cold load, then live-tails. Empty list when nothing is
        # outstanding (the common case).
        pending_elicitations=(
            pending_elicitation_events
            if pending_elicitation_events is not None
            else pending_elicitations.snapshot_for(conv.id)
        ),
        # Replay un-consumed web messages on native-terminal sessions
        # so a client that posted then navigated away / rebound re-
        # hydrates the optimistic bubble. Empty for non-native sessions
        # (their message is already persisted into ``items``).
        pending_inputs=pending_inputs.snapshot_for(conv.id),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        # Replay the latest todo list for claude-native sessions.
        # Populated by _handle_external_session_todos; empty list for
        # non-claude-native sessions or before the first poll tick.
        todos=_session_todos_cache.get(conv.id, []),
        skills=skills or [],
        # Replay terminal spin-up state so a client connecting while the
        # runner is still creating a terminal-first session's terminal
        # sees the Terminal-pill spinner. Populated by the runner SSE
        # relay; absent (False) for non-terminal-first sessions or once
        # the terminal lands / auto-create fails.
        terminal_pending=_session_terminal_pending_cache.get(conv.id, False),
        # Replay managed-sandbox launch progress so a client opening the
        # session mid-launch (the Web UI navigates here immediately
        # after the non-blocking managed create) sees the provisioning
        # indicator. None for sessions without a managed launch and
        # once the launch succeeds; a failed launch is retained with
        # its reason. Populated by _publish_sandbox_status.
        sandbox_status=_session_sandbox_status_cache.get(conv.id),
    )

async def _get_session_snapshot(
    conv_store: ConversationStore,
    session_id: str,
    permission_level: int | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    conversation: Conversation | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    include_items: bool = True,
    runner_exit_reports: RunnerExitReports | None = None,
) -> SessionResponse:
    """
    Read a full session snapshot from the store.

    Centralizes the create/get response building so both endpoints
    return identical projections. The lifecycle ``status`` is
    derived from the relay-fed ``_session_status_cache`` (the tasks
    table has been removed).

    :param conv_store: The conversation store to read from.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param permission_level: The requesting user's numeric level
        on this session, or ``None`` when permissions are disabled.
    :param agent_store: Optional agent store used to look up the
        bound agent's bundle location. ``None`` in legacy call sites
        that don't yet pass it.
    :param agent_cache: Optional agent cache used to load the parsed
        spec from the bundle (provides ``llm_model`` and
        ``context_window``). ``None`` in legacy call sites.
    :param conversation: The already-fetched conversation row to reuse,
        skipping the ``get_conversation`` read. Pass it when the caller
        just authorized the session (which fetched the same row) so the
        snapshot doesn't re-read it. ``None`` reads it here as before.
    :param liveness_lookup: Bulk session-liveness lookup (the server's
        ``_bulk_session_liveness``) used to populate ``runner_online``
        and ``host_online`` on the snapshot. ``None`` (e.g. focused
        tests) leaves both fields ``None`` so the client falls back to
        its ``/health`` poll.
    :param include_items: When ``False``, skip the committed-items read
        and return ``items=[]``. Callers that hydrate the transcript
        through ``GET /sessions/{id}/items`` (the web chat surface)
        pass ``False`` — the items read is the most expensive step of
        the snapshot build and its result would be discarded.
    :returns: The fully populated :class:`SessionResponse`.
    :raises OmnigentError: 404 if no session exists, 500 if the
        underlying conversation has no agent binding
        (see :func:`_build_session_response`).
    """
    conv = conversation
    if conv is None:
        conv = await asyncio.to_thread(conv_store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError(
            "Session not found",
            code=ErrorCode.NOT_FOUND,
        )
    # Return the most recent committed items while preserving the
    # SessionResponse contract that ``items`` is chronological. The
    # store's default page is the oldest 100 (``order="asc"``), which
    # makes long-session reconnects appear stale in clients that use the
    # snapshot directly.
    items: list[ConversationItem] = []
    if include_items:
        items_page = await asyncio.to_thread(
            conv_store.list_items,
            conversation_id=session_id,
            limit=100,
            order="desc",
        )
        items = list(reversed(items_page.data))
    # Resolve the bound runner client once — used for live status (on a
    # status-cache miss) and for runner-owned skill discovery below.
    #
    # Prefer the router (multi-runner deployments wire only
    # ``set_runner_router``; the legacy ``get_runner_client`` singleton
    # stays ``None`` there). Fall back to the legacy singleton for
    # single-runner / in-process tests.
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_client: httpx.AsyncClient | None = None
    runner_router = get_runner_router()
    if runner_router is not None:
        try:
            routed = await runner_router.aclient_for_session_resources(session_id)
            runner_client = routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s on snapshot build",
                session_id,
            )
    if runner_client is None:
        runner_client = get_runner_client()

    status = _session_status_cache.get(session_id)
    if status is None:
        # Cache miss: either the server restarted, or the relay
        # has not yet published the first ``"running"`` event
        # for a freshly bound session (the relay's GET /stream
        # is still in its tunnel handshake). Ask the runner for
        # live status so we don't synthesize a stale ``"idle"``
        # while a turn is actually in flight.
        if runner_client is not None:
            try:
                resp = await runner_client.get(
                    f"/v1/sessions/{session_id}",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    status = resp.json().get("status", "idle")
                    _session_status_cache[session_id] = status
            except httpx.HTTPError:
                _logger.debug(
                    "Runner status query failed for %s",
                    session_id,
                )
        if status is None:
            status = "idle"
    # last_total_tokens and last_task_error come from the context-tokens
    # label written by the forwarder (tasks table has been removed).
    last_total_tokens: int | None = None
    last_task_error: dict[str, str] | None = None
    raw_label = conv.labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY)
    if isinstance(raw_label, str) and raw_label.isdigit():
        last_total_tokens = int(raw_label)
    last_task_error = _last_task_error_from_labels(conv.labels)
    # Runner-crash durability: if the session's bound runner reported an
    # unexpected exit (host.runner_exited → RunnerExitReports), surface the
    # cause as last_task_error so a reload/late-open still renders the error
    # banner — the live session.status:failed push is gone by then. status
    # already reads "failed" from the cache (set by _on_runner_exited). The
    # report is keyed by the CURRENT runner_id, so a successful relaunch
    # (new token-bound runner_id) naturally stops matching. Access is gated
    # by the session-snapshot's own authorization, so the unscoped get is
    # correct here (the report is this session's own runner).
    if runner_exit_reports is not None and conv.runner_id is not None:
        exit_error = runner_exit_reports.get(conv.runner_id)
        if exit_error is not None:
            last_task_error = {"code": "runner_failed_to_start", "message": exit_error}
            status = "failed"
    llm_model: str | None = None
    context_window: int | None = None
    agent_name: str | None = None
    if agent_store is not None and agent_cache is not None and conv.agent_id is not None:
        try:
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is not None:
                agent_name = agent.name
                if agent.bundle_location is not None:
                    # Offload to a worker thread: on a cold cache this fetches
                    # the bundle from the artifact store and parses the spec —
                    # blocking IO that would otherwise stall the single-worker
                    # event loop on every page-load snapshot.
                    loaded = await asyncio.to_thread(
                        agent_cache.load, agent.id, agent.bundle_location
                    )
                    spec = loaded.spec
                    # Prefer the spec's name over the agent row's: a
                    # switch-created session-scoped clone is named
                    # "<builtin> (switch ag_…)" for row disambiguation,
                    # but clients display agent_name verbatim — the spec
                    # carries the clean identity (e.g. "claude-native-ui").
                    if spec.name:
                        agent_name = spec.name
                    llm_model = spec.executor.model
                    # Size the context ring against whatever the next turn
                    # will actually run. spec.executor.context_window only
                    # applies to spec.model, so it's bypassed when an
                    # override is active.
                    effective_model = (
                        conv.model_override if conv.model_override is not None else llm_model
                    )
                    spec_cw = spec.executor.context_window
                    if spec_cw is not None and conv.model_override is None:
                        context_window = spec_cw
                    elif effective_model is not None:
                        from omnigent.llms.context_window import get_model_context_window

                        # Offload to a worker thread: on a cache-cold
                        # provider catalog this does a blocking HTTP fetch
                        # (and litellm registry lookups are CPU-bound), so
                        # running it inline would stall the single-worker
                        # event loop and serialize every concurrent request
                        # behind this snapshot.
                        context_window = await asyncio.to_thread(
                            get_model_context_window, effective_model
                        )
        except Exception:  # noqa: BLE001 — best-effort; missing agent must not break session fetch
            pass
    # Skills are runner-owned: the bound runner discovers them against its
    # own filesystem (bundled skills + host skills under the session's
    # workspace and ``~/.claude/skills/``) — the host where the harness
    # actually executes and may read a skill's local resource files. The
    # server only overlays the result; best-effort, empty when no runner
    # is bound or it can't be reached.
    skills = await _fetch_runner_skills(runner_client, session_id)
    # Dynamic override from the forwarder (real Claude Code window).
    # Only present after the first statusLine tick; before that the
    # spec default applies.
    raw_window_label = conv.labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY)
    if isinstance(raw_window_label, str) and raw_window_label.isdigit():
        observed = int(raw_window_label)
        if observed > 0:
            context_window = observed
    # Resolve strict runner + host liveness for the open-session view.
    # The lookup hits the conversations + hosts tables, so offload it to
    # a worker thread (mirroring _apply_liveness_to_items). Left None on
    # both fields when no lookup is wired (focused tests).
    runner_online: bool | None = None
    host_online: bool | None = None
    if liveness_lookup is not None:
        liveness = await asyncio.to_thread(liveness_lookup, [session_id])
        result = liveness.get(session_id)
        if result is not None:
            runner_online = result.runner_online
            host_online = result.host_online
    # Subtree usage (this session + its sub-agent descendants) so the
    # displayed cost includes sub-agents — a codex/claude sub-agent's spend
    # is persisted on its own child conversation, not the parent's, so the
    # parent's own session_usage would under-report. Off the event loop
    # because it pages the conversation tree from the store.
    subtree_usage = await asyncio.to_thread(load_session_usage, conv.id, conv_store)
    return _build_session_response(
        conv,
        items,
        status,
        permission_level,
        llm_model=llm_model,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        last_task_error=last_task_error,
        agent_name=agent_name,
        skills=skills,
        runner_online=runner_online,
        host_online=host_online,
        pending_elicitation_events=await asyncio.to_thread(
            _pending_elicitation_snapshot_for_session,
            conv_store,
            conv,
        ),
        subtree_usage=subtree_usage,
    )

