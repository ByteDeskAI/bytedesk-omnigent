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

def _ancestor_session_ids(
    conv_store: ConversationStore,
    session_id: str,
) -> list[str]:
    """
    Return ancestor session ids for a session, nearest parent first.

    :param conv_store: Store used to read conversation parent links.
    :param session_id: Session to walk upward from, e.g.
        ``"conv_child123"``.
    :returns: Ancestor ids in parent-to-root order. Empty when the
        session is top-level or missing.
    """
    ancestors: list[str] = []
    seen = {session_id}
    current = conv_store.get_conversation(session_id)
    while current is not None and current.parent_conversation_id is not None:
        parent_id = current.parent_conversation_id
        if parent_id in seen:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = conv_store.get_conversation(parent_id)
    return ancestors

def _descendant_sessions(
    conv_store: ConversationStore,
    session_id: str,
) -> list[Conversation]:
    """
    Return descendant sub-agent conversations for a session.

    :param conv_store: Store used to list conversations.
    :param session_id: Ancestor session id, e.g. ``"conv_root123"``.
    :returns: Sub-agent conversations below ``session_id``. Empty
        for sessions with no descendants.
    """
    descendants: list[Conversation] = []
    queue: deque[str] = deque([session_id])
    seen = {session_id}
    while queue:
        parent_id = queue.popleft()
        after: str | None = None
        while True:
            page = conv_store.list_conversations(
                kind="sub_agent",
                parent_conversation_id=parent_id,
                limit=100,
                after=after,
            )
            for child in page.data:
                if child.id in seen:
                    continue
                seen.add(child.id)
                descendants.append(child)
                queue.append(child.id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
    return descendants

def _find_subagent_child_by_title(
    conversation_store: ConversationStore,
    parent_id: str,
    title: str,
) -> Conversation | None:
    """
    Look up an existing sub-agent child by its exact title.

    Recovery path for duplicate-title races: when ``create_conversation``
    trips the ``(parent_conversation_id, title)`` unique index but the
    label-based idempotency lookup missed — the original POST crashed
    after creating the row and before ``set_labels`` ran — the row can
    only be found by the title itself. Native sub-agent titles embed the
    stable harness-side id (e.g. ``"Explore:a5c7effac5a9a35ab"``,
    ``"codex-native-ui-subagent:<thread_id>"``), so an exact title match
    under the same parent identifies the same physical sub-agent.

    :param conversation_store: Store to query.
    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param title: Exact child title, e.g. ``"Explore:a5c7effac5a9a35ab"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row under *parent_id* carries that title.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.title == title:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id

async def _enrich_idle_status_with_subagent_output(
    data: dict[str, Any],
    status: str,
    session_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Attach a native sub-agent's durable assistant text to an idle status edge.

    Shared by both native sub-agent delivery paths (the codex
    ``external_session_status`` POST handler and the claude-native relay
    forward) so the parent inbox result carries the child's output. Native
    harnesses mirror transcript items to the store, not runner memory, so the
    text is read here and forwarded with the idle edge.

    :param data: The ``external_session_status`` ``data`` to enrich, e.g.
        ``{"status": "idle"}``.
    :param status: Status edge; only ``"idle"`` is enriched.
    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conversation_store: Store read for the child's assistant text.
    :returns: ``data`` with ``"output"`` added when an idle edge has a
        persisted assistant message; otherwise unchanged.
    """
    if status != "idle":
        return data
    output = await asyncio.to_thread(
        _latest_assistant_text_from_store,
        conversation_store,
        session_id,
    )
    if output is None:
        return data
    return {**data, "output": output}

async def _wake_parent_for_blocked_child(
    parent_id: str,
    child: Conversation,
    notice: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> bool:
    """
    Deliver a parent-wake notice when a sub-agent blocks on an approval.

    Posts the ``[System: …]`` notice as a synthetic user message to the
    parent's ``POST /v1/sessions/{id}/events`` — the same path the runner's
    terminal-completion wake uses, so it starts a continuation turn (idle
    parent) or coalesces with pending input (busy parent). Best-effort: a
    missing parent, missing runner, or transport error is logged and swallowed
    (a dropped wake is no worse than the pre-fix no-wake baseline), but the
    *outcome* is reported back so the notifier can release its per-block
    debounce and let a later publish retry rather than silencing the block.

    :param parent_id: Parent session id, e.g. ``\"conv_parent123\"``.
    :param child: The blocked child :class:`Conversation`; used only for its
        label/id in the notice and logs.
    :param notice: The ``[System: …]`` text to inject into the parent.
    :param conversation_store: Used to load the parent :class:`Conversation`
        and persist the synthetic user message item.
    :param runner_router: Router used to resolve the parent's bound
        runner. ``None`` in in-process setups (the runtime singleton is
        consulted as a fallback).
    :returns: ``True`` when the notice was dispatched to the parent's runner;
        ``False`` when delivery could not happen (parent gone, no runner bound,
        or the forward raised a transport error).
    """
    parent_conv = await asyncio.to_thread(conversation_store.get_conversation, parent_id)
    if parent_conv is None:
        # Parent vanished between publish and wake (cascading-delete race).
        _logger.debug(
            "subagent block notifier: parent %s missing; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    runner_client = await _get_runner_client(parent_id, runner_router)
    if runner_client is None:
        # WARNING (not DEBUG): an unbound parent is the transient-miss case the
        # notifier retries — surface it rather than burying it as routine.
        _logger.warning(
            "subagent block notifier: no runner bound for parent %s; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    # Ensure the parent's SSE relay is live so the wake turn's output is
    # persisted (parity with post_event).
    _ensure_runner_relay(
        parent_id,
        parent_conv.runner_id,
        runner_client,
        conversation_store,
    )
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": notice}],
        },
    )
    try:
        # None args: a system notice carries no agent/files/artifacts; the runner
        # recomputes has_mcp_servers from the parent's cached spec.
        await _dispatch_session_event_to_runner(
            parent_id,
            parent_conv,
            body,
            conversation_store,
            runner_client,
            agent_name=None,
            file_store=None,
            artifact_store=None,
            runner_router=runner_router,
        )
    except (httpx.HTTPError, OmnigentError):
        _logger.warning(
            "subagent block wake POST failed for parent=%s child=%s",
            parent_id,
            child.id,
            exc_info=True,
        )
        return False
    return True

def configure_subagent_block_notifier(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> Callable[[], None]:
    """
    Install the parent-wake notifier on the elicitation publish path.

    Wires :class:`SubagentBlockNotifier` into
    :mod:`omnigent.runtime.pending_elicitations` so a sub-agent that
    blocks on an approval immediately wakes its immediate parent through
    the same ``/events`` ingest path the runner-side terminal-completion
    wake already uses (see
    :func:`_wake_parent_for_blocked_child`). Top-level sessions (no
    parent) are no-ops; multi-user safety is inherent because the wake
    is delivered to the recorded ``parent_conversation_id`` only, never
    fanned out to collaborators or unrelated sessions.

    :param conversation_store: Store used to resolve a child's
        ``parent_conversation_id`` and to persist the wake message.
    :param runner_router: Router used by the wake to reach the parent's
        bound runner. ``None`` in in-process setups.
    :returns: A callable that uninstalls the observer and cancels any
        in-flight wake futures. Call from the lifespan teardown.
    """
    from omnigent.runtime import pending_elicitations as _pending_elicitations
    from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier

    loop = asyncio.get_running_loop()

    async def _wake_dispatch(parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Deliver one wake notice (the notifier's injected dispatch).

        :param parent_id: Parent session id.
        :param child: The blocked child :class:`Conversation`.
        :param notice: Pre-formatted ``[System: …]`` text.
        :returns: ``True`` when the notice reached the parent's runner,
            ``False`` when it could not be delivered (so the notifier
            releases the debounce and a re-publish can retry).
        """
        return await _wake_parent_for_blocked_child(
            parent_id,
            child,
            notice,
            conversation_store=conversation_store,
            runner_router=runner_router,
        )

    notifier = SubagentBlockNotifier(
        conversation_store=conversation_store,
        wake_dispatch=_wake_dispatch,
        loop=loop,
    )
    _pending_elicitations.set_elicitation_observer(notifier.observe)

    def _uninstall() -> None:
        """Remove the observer and cancel any outstanding wake futures."""
        _pending_elicitations.set_elicitation_observer(None)
        notifier.close()

    return _uninstall

def _child_session_summary_from_conversation(
    conv: Conversation,
    parent_session_id: str,
    last_message_preview: str | None,
) -> ChildSessionSummary:
    """
    Build a :class:`ChildSessionSummary` from a child conversation.

    Parses the canonical sub-agent title format
    ``"{agent_type}:{session_name}"`` written by
    :func:`omnigent.tools.builtins.spawn._spawn_one`, plus the
    3-segment ``"ui:{agent_name}:{user_label}"`` form written by the
    Web UI "Add agent" flow (surfaced as ``tool={agent_name}`` and
    ``session_name={user_label}``). Tolerates malformed/legacy rows:
    if the title is ``None`` or has no colon, ``tool`` falls back to
    the raw title and ``session_name`` is ``None`` — the row is still
    surfaced so debug views can investigate.

    ``busy`` is derived from the relay-fed ``_session_status_cache``
    (the tasks table has been removed). ``agent_id`` and ``agent_name``
    are read from the conversation row directly.

    :param conv: A child :class:`Conversation` row
        (``kind="sub_agent"``) from
        :meth:`ConversationStore.list_conversations`.
    :param parent_session_id: The parent session id from the
        route, e.g. ``"conv_parent987"``. Passed in rather than
        re-reading from ``conv.parent_conversation_id`` to keep
        the helper indifferent to legacy rows where the FK might
        be missing.
    :param last_message_preview: Preview text derived from a batched
        child-message lookup, or ``None`` when no visible message exists.
    :returns: A populated :class:`ChildSessionSummary`.
    """
    display_title = title_without_closed_marker(conv.title)
    labels = labels_with_closed_status(conv.labels, conv.title)
    tool: str | None
    session_name: str | None
    if _is_codex_native_subagent(conv):
        # Codex-native child: surface the Codex-assigned nickname/role as
        # ``tool`` and the raw thread id as ``session_name`` for correlation.
        tool = _codex_subagent_display_tool(labels)
        session_name = labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY)
    elif display_title and ":" in display_title:
        head, _, tail = display_title.partition(":")
        if head == _UI_ADDED_AGENT_TITLE_PREFIX and ":" in tail:
            # User-added agent: "ui:<agent_name>:<user_label>". Surface the
            # bound agent as ``tool`` and the user's label as ``session_name``
            # so the Agents rail renders it like any other child row.
            agent_name, _, user_label = tail.partition(":")
            tool = agent_name
            session_name = user_label
        else:
            tool = head
            session_name = tail
    else:
        tool = display_title or None
        session_name = None

    # Derive busy from the relay-fed cache; tasks table is gone.
    cached_status = _session_status_cache.get(conv.id)
    if cached_status in ("running", "waiting"):
        busy = True
    else:
        busy = False
    last_task_error = _last_task_error_from_labels(labels)
    current_task_status = (
        "failed" if cached_status == "failed" or last_task_error is not None else None
    )

    # For Codex children, fall back to the prompt label as preview when the
    # real transcript has not arrived yet — avoids synthesizing a user message
    # just so the rail has something to show.
    if last_message_preview is None and _is_codex_native_subagent(conv):
        raw_prompt = labels.get(_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY)
        if raw_prompt:
            collapsed = " ".join(raw_prompt.split())
            last_message_preview = collapsed[:_CHILD_PREVIEW_LIMIT] or None

    return ChildSessionSummary(
        id=conv.id,
        parent_session_id=parent_session_id,
        title=display_title,
        tool=tool,
        session_name=session_name,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        # agent_id comes from the conversation row; agent_name and task_id
        # are no longer available from the (removed) tasks table.
        agent_id=conv.agent_id,
        agent_name=None,
        current_task_id=None,
        current_task_status=current_task_status,
        busy=busy,
        labels=labels,
        last_task_error=last_task_error,
        last_message_preview=last_message_preview,
        # Surface the sub-agent's parked-elicitation count from the same
        # in-memory index that feeds the sidebar badge, so the Agents
        # rail can flag a child that's awaiting user input.
        pending_elicitations_count=pending_elicitations.count_for(conv.id),
    )

async def _child_session_summaries_from_conversations(
    children: list[Conversation],
    parent_session_id: str,
    conv_store: ConversationStore,
) -> list[ChildSessionSummary]:
    """
    Build child summaries with one batched message-preview lookup.

    ``ChildSessionSummary.last_message_preview`` needs the latest visible
    message per child. Loading those by calling ``list_items`` once per
    child blocks the event loop and creates N+1 database traffic. This
    helper reads newest message items for all child ids in a worker
    thread, computes previews in memory, then builds summaries without
    further store access.

    :param children: Child conversation rows from
        ``list_conversations(kind="sub_agent")``.
    :param parent_session_id: Parent session id, e.g. ``"conv_parent987"``.
    :param conv_store: Conversation store used for the batched message read.
    :returns: One :class:`ChildSessionSummary` per input child, preserving
        input order.
    """
    if not children:
        return []
    child_ids = [child.id for child in children]
    message_items_by_child = await asyncio.to_thread(
        conv_store.list_latest_message_items_for_conversations,
        child_ids,
        10,
    )
    previews = {
        child_id: _latest_message_preview(message_items)
        for child_id, message_items in message_items_by_child.items()
    }
    return [
        _child_session_summary_from_conversation(
            child,
            parent_session_id,
            previews.get(child.id),
        )
        for child in children
    ]

