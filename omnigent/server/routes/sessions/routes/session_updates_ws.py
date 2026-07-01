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

def register_session_updates_ws(
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
        # ── WS /sessions/updates ────────────────────────────────────

        async def _fetch_watched_items(
            watched: list[str],
            user_id: str | None,
        ) -> list[dict[str, Any]]:
            """
            Build current list-item payloads for the watched ids.

            Reads exactly the same sources as ``GET /v1/sessions`` (the
            relay-fed status cache plus the conversation store) and enforces
            per-session read access: ids the user cannot access, that don't
            exist, or that aren't sessions (no ``agent_id``) are silently
            omitted. This is the pull the session-updates stream diffs each
            interval — it is a drop-in for the client's former list poll, not
            a new event source, so it carries no new cross-replica semantics.

            When ``liveness_lookup`` is wired, each payload also carries
            ``runner_online`` and ``host_online`` (the same values
            ``GET /health`` and ``GET /v1/sessions`` return), so the client
            can drop its per-session ``/health`` poll for watched sessions.

            :param watched: Conversation ids the client is currently
                displaying, e.g. ``["conv_abc", "conv_def"]``. Already
                deduplicated and length-capped by the caller.
            :param user_id: The authenticated requesting user, or ``None``
                when permissions are disabled, e.g. ``"alice@example.com"``.
            :returns: One JSON-ready dict per accessible, existing watched
                session, in no particular order.
            """
            if not watched:
                return []
            if permission_store is not None:
                perms_by_conv = await asyncio.to_thread(permission_store.list_for_sessions, watched)
                user_is_admin = (
                    await asyncio.to_thread(permission_store.is_admin, user_id)
                    if user_id is not None
                    else False
                )
                accessible = [
                    cid
                    for cid in watched
                    if _permission_level_from_grants(
                        user_id, perms_by_conv.get(cid, []), user_is_admin
                    )
                    is not None
                ]
            else:
                perms_by_conv = {}
                user_is_admin = False
                accessible = list(watched)
            if not accessible:
                return []

            def _load_sessions(ids: list[str]) -> list[Conversation]:
                """Bulk-load the accessible conversations that are sessions
                (non-null ``agent_id``) in one batched store call, preserving
                the caller's id order for deterministic output."""
                by_id = conversation_store.get_conversations(ids)
                return [
                    conv
                    for cid in ids
                    if (conv := by_id.get(cid)) is not None and conv.agent_id is not None
                ]

            convs = await asyncio.to_thread(_load_sessions, accessible)
            if not convs:
                return []
            unique_agent_ids = list({c.agent_id for c in convs if c.agent_id is not None})
            conv_ids = [c.id for c in convs]
            agent_names_by_id, child_ids_by_parent, comments_fingerprints = await asyncio.gather(
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent,
                    conv_ids,
                ),
                _comments_fingerprints_for(conv_ids),
            )
            pending_counts = pending_elicitations.counts_for(conv_ids)
            agent_display_names_by_id = await asyncio.to_thread(
                _agent_display_names_for, unique_agent_ids, agent_store, agent_cache
            )
            items = [
                _build_session_list_item(
                    conv,
                    agent_names_by_id=agent_names_by_id,
                    agent_display_names_by_id=agent_display_names_by_id,
                    grants=perms_by_conv.get(conv.id, []),
                    user_id=user_id,
                    user_is_admin=user_is_admin,
                    permissions_enabled=permission_store is not None,
                    pending_count=pending_counts.get(conv.id, 0),
                    child_session_ids=child_ids_by_parent[conv.id],
                    comments_fingerprint=comments_fingerprints.get(conv.id),
                )
                for conv in convs
            ]
            await _apply_liveness_to_items(items, liveness_lookup)
            # Full-row dumps (every field, nulls included) — NOT exclude_none. The
            # stream is a diff source: the client overlays these onto its cached
            # rows, so a field that cleared to null must arrive as an explicit null
            # (an absent key would leave the stale value in the cache). The client
            # converts null → undefined on apply, so a cleared field lands in the
            # same shape GET /v1/sessions produces (absent), and the
            # ``permission_level === null`` full-access sentinel in the web sidebar
            # is never tripped by a streamed null. The GET list endpoint keeps
            # exclude_none — it replaces whole pages, so it has nothing to clear.
            return [item.model_dump() for item in items]

        @router.websocket("/sessions/updates")
        async def session_updates(websocket: WebSocket) -> None:
            """
            Push session-list changes for a client-supplied watch-set.

            Replaces the web app's 4 s HTTP poll of ``GET /v1/sessions``
            with one persistent connection. Protocol (JSON text frames):

            - **client → server**:
              ``{"type": "watch", "session_ids": [...]}`` — the ids the
              client is currently displaying. Sent on connect and re-sent
              whenever the visible set changes (scroll / filter /
              pagination); it fully replaces the prior watch-set. Unknown
              message shapes are ignored for forward compatibility.
            - **server → client**:
              ``{"type": "snapshot", "items": [SessionListItem, ...]}`` once
              per ``watch`` (full state for the new set), then
              ``{"type": "changed", "items": [...]}`` /
              ``{"type": "removed", "ids": [...]}`` deltas as watched
              sessions change, and ``{"type": "heartbeat"}`` when idle.

            Watched-row freshness is pull-based — each interval the server
            re-reads the watched ids (the same read ``GET /v1/sessions`` does)
            and emits only what changed. *Discovery* of sessions the client
            isn't watching yet (created / forked / shared elsewhere) is instead
            push-based: a ``session_added`` event on this user's
            :mod:`user_session_stream` channel makes the server push the new
            session as a ``changed`` frame, which the client reconciles into the
            sidebar. Together these mean an idle list makes zero HTTP polls yet a
            new session still appears within a tick of being created.

            :param websocket: The incoming FastAPI :class:`WebSocket`.
            """
            user_id = auth_provider.get_user_id(websocket) if auth_provider is not None else None
            # When permissions are enabled, an unauthenticated socket can see
            # nothing useful and must not be allowed to probe ids; reject the
            # handshake (mirrors the terminal-attach authorization gate).
            if permission_store is not None and user_id is None:
                raise WebSocketException(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="authentication required",
                )
            await websocket.accept()

            watched: list[str] = []
            # Last SessionListItem dump sent per id, used to diff. Keyed only
            # by currently-watched ids; pruned when the watch-set narrows.
            last_sent: dict[str, dict[str, Any]] = {}
            last_send_monotonic = time.monotonic()
            # Serializes the read-diff-send-update critical section between the
            # reader (snapshot on watch) and the ticker (interval deltas) so
            # they never interleave updates to ``last_sent``.
            emit_lock = asyncio.Lock()

            async def _send(frame: dict[str, Any]) -> None:
                """
                Serialize and send one frame, stamping the last-send time so
                the heartbeat timer measures idleness from the last real send.

                :param frame: The outgoing frame, e.g.
                    ``{"type": "changed", "items": [...]}``. Sent as JSON text.
                """
                nonlocal last_send_monotonic
                await websocket.send_text(json.dumps(frame))
                last_send_monotonic = time.monotonic()

            async def _emit_snapshot() -> None:
                """Send a full snapshot for the current watch-set and reset the
                diff baseline to it."""
                items = await _fetch_watched_items(watched, user_id)
                dumps = {item["id"]: item for item in items}
                last_sent.clear()
                last_sent.update(dumps)
                await _send({"type": "snapshot", "items": list(dumps.values())})

            async def _emit_deltas() -> None:
                """Diff the watched ids against the last frame and send only the
                changes; emit a heartbeat when nothing changed but the link has
                been idle."""
                nonlocal last_send_monotonic
                if watched:
                    items = await _fetch_watched_items(watched, user_id)
                    current = {item["id"]: item for item in items}
                    changed = [dump for cid, dump in current.items() if last_sent.get(cid) != dump]
                    # Removed = a still-watched id that no longer resolves (lost
                    # access or deleted). De-watched ids are pruned silently
                    # below, not reported as removed.
                    removed = [cid for cid in watched if cid not in current and cid in last_sent]
                    last_sent.clear()
                    last_sent.update(current)
                    if changed:
                        await _send({"type": "changed", "items": changed})
                    if removed:
                        await _send({"type": "removed", "ids": removed})
                if time.monotonic() - last_send_monotonic >= _SESSION_UPDATES_HEARTBEAT_INTERVAL_S:
                    await _send({"type": "heartbeat"})

            async def _reader() -> None:
                """Apply incoming watch-set updates and snapshot each one."""
                nonlocal watched
                while True:
                    raw = await websocket.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict) or msg.get("type") != "watch":
                        # Forward-compatible: ignore frames we don't understand.
                        continue
                    ids = msg.get("session_ids")
                    if not isinstance(ids, list):
                        continue
                    # Dedupe preserving order, keep only strings. Dedupe fully
                    # first, then cap — so the truncation count below is the real
                    # number of distinct ids dropped, not skewed by duplicates that
                    # happen to sit past the cap.
                    deduped: list[str] = []
                    unique: set[str] = set()
                    for cid in ids:
                        if isinstance(cid, str) and cid not in unique:
                            unique.add(cid)
                            deduped.append(cid)
                    if len(deduped) > _SESSION_UPDATES_MAX_WATCHED:
                        # Ids past the cap get no push updates and are never reported
                        # "removed" (they aren't watched). The client's low-rate list
                        # reconciliation still covers them, but log the silent drop so
                        # an oversized watch-set is diagnosable rather than invisible.
                        _logger.warning(
                            "session-updates watch-set truncated to %d of %d distinct ids "
                            "for user %r; ids beyond the cap rely on list-poll reconciliation",
                            _SESSION_UPDATES_MAX_WATCHED,
                            len(deduped),
                            user_id,
                        )
                        deduped = deduped[:_SESSION_UPDATES_MAX_WATCHED]
                    # The watched set after capping — used to prune baselines for ids
                    # the client no longer watches (including any just truncated).
                    watched_set = set(deduped)
                    async with emit_lock:
                        watched = deduped
                        # Drop baselines for ids no longer watched so they
                        # can't surface as spurious "removed" later.
                        for stale in [cid for cid in last_sent if cid not in watched_set]:
                            del last_sent[stale]
                        await _emit_snapshot()

            async def _ticker() -> None:
                """Emit deltas / heartbeats on a fixed interval."""
                while True:
                    await asyncio.sleep(_SESSION_UPDATES_RESCAN_INTERVAL_S)
                    async with emit_lock:
                        try:
                            await _emit_deltas()
                        except WebSocketDisconnect:
                            # The client went away mid-send — the normal terminal
                            # condition. Propagate so the stream tears down and the
                            # reader/ticker pair is cancelled.
                            raise
                        except Exception:  # noqa: BLE001 — a transient tick failure must not tear down a live stream
                            # A transient store/DB read failure must not kill a live
                            # stream and force every watcher to reconnect +
                            # re-snapshot. Log it and try again next interval; the
                            # diff is recomputed from scratch each tick, so a skipped
                            # tick costs at most one delayed delta. (CancelledError
                            # is not an Exception subclass, so cancellation still
                            # propagates.)
                            _logger.warning(
                                "session-updates delta tick failed; retrying next interval",
                                exc_info=True,
                            )

            async def _discovery() -> None:
                """Push sessions newly made accessible to this user — created,
                forked, or shared from elsewhere — so they enter the sidebar
                without a list poll.

                Such ids are NOT in the client's watch-set (the client doesn't
                know about them yet), so the per-interval diff can't surface them.
                This reacts to the create/grant event instead: it fetches the one
                announced id (access-checked, same as the watch path) and pushes
                it. The client reconciles the unknown id into its cache, then
                re-sends its watch-set including it, after which it is tracked
                like any normal watched row. Idle users with no new sessions
                receive nothing — so the zero-traffic property holds."""
                async for evt in user_session_stream.subscribe(_discovery_key(user_id)):
                    if not isinstance(evt, dict) or evt.get("type") != "session_added":
                        continue
                    sid = evt.get("session_id")
                    if not isinstance(sid, str):
                        continue
                    async with emit_lock:
                        # Already watched ⇒ the normal diff already covers it.
                        if sid in watched:
                            continue
                        try:
                            items = await _fetch_watched_items([sid], user_id)
                            if items:
                                await _send({"type": "changed", "items": items})
                        except WebSocketDisconnect:
                            # Client gone mid-send — propagate to tear the stream down.
                            raise
                        except Exception:  # noqa: BLE001 — a failed discovery push must not kill a live stream
                            # A transient read/send failure for one announcement
                            # must not drop the whole stream; the session is still
                            # discoverable on the client's next list reconcile.
                            _logger.warning(
                                "session-updates discovery push failed for %r; "
                                "falling back to list reconcile",
                                sid,
                                exc_info=True,
                            )

            reader_task = asyncio.create_task(_reader(), name="session-updates-reader")
            ticker_task = asyncio.create_task(_ticker(), name="session-updates-ticker")
            discovery_task = asyncio.create_task(_discovery(), name="session-updates-discovery")
            try:
                done, pending = await asyncio.wait(
                    {reader_task, ticker_task, discovery_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in done:
                    exc = task.exception()
                    # A client disconnect is the normal terminal condition; any
                    # other exception is a real bug worth surfacing in logs.
                    if exc is not None and not isinstance(exc, WebSocketDisconnect):
                        _logger.warning("session-updates stream task crashed: %r", exc)
            finally:
                with contextlib.suppress(RuntimeError):
                    await websocket.close()

