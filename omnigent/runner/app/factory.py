"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import mimetypes
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.codex_native_app_server import CodexAppServerClient
    from omnigent.runner.cost_advisor import AdvisorTurnResult

    # Boundary payload TypedDicts (sweep-2 BDP-2366). Imported type-only so
    # the runtime ``app`` <-> ``tool_dispatch`` import stays lazy (the cycle
    # both modules already break with function-level imports).
    from omnigent.runner.tool_dispatch import (
        SessionSnapshotPayload,
        SubagentInboxPayload,
    )
    from omnigent.terminals.registry import TerminalListEntry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.model_override import validate_model_override
from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runner.subagent_status import (
    _TERMINAL as _SUBAGENT_TERMINAL_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
    TerminalStatus,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

_logger = logging.getLogger(__name__)
from fastapi import FastAPI
from ._constants import *
from ._state import *
from ._dispatch import *  # noqa: F403
from ._forwarders import *  # noqa: F403
from ._harness import *  # noqa: F403
from ._helpers import *  # noqa: F403
from ._policy import *  # noqa: F403
from ._streaming import *  # noqa: F403
from ._subagents import *  # noqa: F403
from ._terminals import *  # noqa: F403
from ._timers import *  # noqa: F403
from ._tools import *  # noqa: F403

def create_runner_app(
    *,
    process_manager: HarnessProcessManager | None = None,
    spec_resolver: SpecResolver | None = None,
    server_client: httpx.AsyncClient,
    terminal_registry: Any | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    runner_workspace: Path | None = None,
    per_session_workspace: bool = True,
    mcp_manager: Any | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    """Build a fresh runner FastAPI app.

    :param process_manager: Pre-started HarnessProcessManager.
        ``None`` → scaffold mode (501 stubs).
    :param spec_resolver: Async callback ``(agent_id) -> AgentSpec | None``.
        For in-process: wraps the server's agent cache.
        For out-of-process: wraps HTTP fetch to GET /v1/agents/{id}/contents.
        ``None`` → runner falls back to body-supplied hints (test path).
    :param server_client: httpx.AsyncClient pointed at the AP
        server's public API. Used by the runner for
        elicitation/approval forwarding.
        In-process: pointed at the Omnigent ASGI app.
        Out-of-process: pointed at the server's HTTP URL.
    :param terminal_registry: TerminalRegistry instance for
        runner-local terminal tool dispatch (Phase 2).
        ``None`` → terminal tools relay upstream.
    :param runner_workspace: Optional local workspace path passed
        by the CLI when the runner owns filesystem tools for a
        remote app server session.
    :param per_session_workspace: ``True`` (default) isolates each
        session under a subdirectory of *runner_workspace*.
        Single-user CLI runners pass ``False`` so the agent sees the
        project root. No effect when *runner_workspace* is ``None``.
    :param mcp_manager: Optional :class:`RunnerMcpManager` owning
        this runner's MCP pool. ``None`` skips MCP injection
        (test path).
    :param auth_token: Optional bearer token that callers must
        present in the ``Authorization`` header.  When set, every
        request except ``GET /health`` is rejected with 401 if
        the token is missing or wrong.  ``None``
        disables auth (in-process / test path).
    """
    import hmac

    app = FastAPI(title="omnigent-runner")

    from omnigent.identity.verifiers import HmacAssertionVerifier

    # Acting-identity verifier (BDP-2422): decodes the signed
    # X-Omnigent-Acting-Identity carrier on inbound tool dispatch. from_env() is a
    # no-secret (fail-closed) verifier when unconfigured, so an absent secret simply
    # leaves acting_identity None (today's behaviour). Tests may override it.
    app.state.assertion_verifier = HmacAssertionVerifier.from_env()

    # Runner-side auth middleware.
    if auth_token is not None:
        _expected_token = auth_token

        @app.middleware("http")
        async def _runner_auth_middleware(request: Request, call_next: Any) -> Response:
            """Reject requests without a valid bearer token.

            Requests arriving through the WebSocket tunnel have
            ASGI client ``("tunnel", 0)`` and are already
            authenticated by the tunnel handshake — exempt them.

            :param request: Incoming HTTP request.
            :param call_next: Next middleware / route handler.
            :returns: The response, or 401 on auth failure.
            """
            if request.url.path == "/health":
                return await call_next(request)
            # Tunnel-dispatched requests are already authenticated
            # by the WebSocket tunnel registration handshake.
            client = request.scope.get("client")
            if client is not None and client[0] == "tunnel":
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[7:]
            else:
                provided = ""
            if not provided or not hmac.compare_digest(provided, _expected_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing runner auth token"},
                )
            return await call_next(request)

    # Set the terminal registry as the runtime global so ToolManager
    # can find it when constructing tool schemas. The runner already
    # owns and dispatches terminal tools — this just lets ToolManager
    # register them for schema extraction.
    if terminal_registry is not None:
        from omnigent.runtime import _globals as _rt_globals

        _rt_globals._terminal_registry = terminal_registry

    _version_cache: dict[str, int] = {}  # conversation_id → last seen agent_version
    _spec_cache: dict[str, Any] = {}  # agent_id → cached AgentSpec for terminal tools
    _resp_to_conv: dict[str, str] = {}  # harness response_id → conversation_id
    _session_start_cache: dict[str, float] = {}  # session_id → registered start time
    _session_spec_cache: dict[str, Any | None] = {}  # session_id → session AgentSpec
    # Single source for the session's server snapshot. created_at,
    # workspace, and agent_id are all projected out of one
    # GET /v1/sessions/{id}; the projection caches above/below are
    # populated from here. Guarded by per-session locks so a startup
    # burst of concurrent readers shares one fetch instead of stampeding.
    _session_snapshot_cache: dict[str, _SessionSnapshot] = {}  # session_id → snapshot
    _session_snapshot_locks: dict[str, asyncio.Lock] = {}  # session_id → snapshot fetch lock
    _session_spec_locks: dict[str, asyncio.Lock] = {}  # session_id → spec resolution lock
    # session_id → merged (bundled + host) skills, discovered against
    # this runner's filesystem. Skills are runner-owned: the walk runs
    # once per session lifetime and is dropped in ``delete_session``.
    _session_skills_cache: dict[str, list[SkillSpec]] = {}
    _session_workspace_cache: dict[str, str | None] = {}  # session_id → workspace path
    _session_agent_ids = _session_agent_ids_ref  # shared with module-level get_session_agent_id
    # Sub-agent name per session. Set from POST /v1/sessions body
    # for child sessions. _run_turn_bg uses this to resolve the
    # sub-spec from the parent's spec tree.
    _session_sub_agent_names: dict[str, str] = {}
    _session_tool_schemas: dict[str, list[dict[str, Any]]] = {}  # session_id → cached tool schemas
    # session_id → the brain model the cost advisor last APPLIED (optimize
    # mode). Carried forward on conversational turns so the brain doesn't
    # flap back to the spec/gateway default between advised turns; the
    # claude-sdk executor only re-runs set_model when the model changes.
    _session_advisor_applied_model: dict[str, str] = {}
    # Per-session comment-tool relay for claude-native sessions. Value is a
    # ClaudeNativeToolRelay handle; ``Any`` avoids importing the class at
    # module load time. Started when the Claude terminal launches (with a
    # first-turn fallback) and closed when the session is deleted.
    _session_comment_relays: dict[str, Any] = {}
    _codex_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _pi_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _grok_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Per-session lock guarding the claude-native terminal auto-create in
    # ``create_session``. Two ``POST /v1/sessions`` calls can land
    # concurrently on a host-launched runner — ``_on_runner_connect``
    # (server/app.py) fires one on every tunnel connect, and the message
    # path's relaunch handshake fires another — so the check-and-create
    # must serialize or both pass the "no terminal yet" test and double
    # launch (409 / rotation loop).
    _claude_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Same guard for the Omnigent REPL (``omnigent attach``) terminal
    # auto-created for non-native SDK sessions.
    _repl_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Turn sequencing (SESSION_REARCHITECTURE Step 5 / SESSION_STEERING_MIGRATION Step 1)
    _active_turns: dict[str, asyncio.Task[None] | None] = {}
    _session_message_buffers: dict[str, list[dict[str, Any]]] = {}
    # Per-conversation message-ingest ordering (RUNNER_MESSAGE_INGEST.md
    # Part A). Each inbound ``message`` event takes a monotonic arrival
    # sequence from ``_ingest_next_seq`` (read-incremented synchronously,
    # so it reflects arrival order), then waits at a FIFO gate until every
    # earlier-arriving message for that conversation has finished its
    # turn-vs-buffer decision (``_ingest_now_serving`` is the sequence
    # currently allowed to proceed; ``_ingest_cond`` wakes waiters). This
    # makes turn ordering follow arrival order, not content-resolution
    # latency — a slow-resolving message can no longer be overtaken.
    _ingest_next_seq: dict[str, int] = {}
    _ingest_now_serving: dict[str, int] = {}
    _ingest_cond: dict[str, asyncio.Condition] = {}
    # Closure-local (one per app instance — a module global would leak stale
    # interrupt flags between distinct create_runner_app() instances in the
    # same process). Exposed on app.state below for test inspection.
    _interrupted_sessions: set[str] = set()
    app.state.interrupted_sessions = _interrupted_sessions
    _background_tasks: set[asyncio.Task[Any]] = set()
    # Parent sessions with an outstanding sub-agent wake POST. Debounces a
    # fan-out's completions: while a parent's wake is outstanding, further
    # child completions skip posting another /events notice (they still land
    # in the inbox, which one wake turn drains). Cleared when the parent
    # starts processing a turn, so a child completion that lands during that
    # turn can schedule the next wake instead of being stranded in the inbox.
    _subagent_wake_pending: set[str] = set()
    # Pending policy-ASK Futures are now owned by
    # ``omnigent.runner.pending_approvals`` so the runner-side
    # policy gate (``omnigent.runner.tool_dispatch``) can register
    # and wait without threading a closure-local dict through every
    # dispatch entry point. The session-event handler below still
    # resolves Futures by elicitation_id; it just routes through the
    # shared module instead of a closure local.

    # Per-session in-memory conversation history. Loaded from the
    # server on the first turn, then appended locally as events
    # flow through proxy_stream. Each entry is a harness input
    # item: {type: "message", role: "user"|"assistant", content: [...]}.
    _session_histories = _session_histories_ref
    # Per-session compaction state: known context window + model.
    # Populated at session creation from litellm registry lookup.
    _compaction_contexts: dict[str, dict[str, Any]] = {}
    # Last server-persisted item ID per session — cursor for
    # incremental catch-up scans (Step 8.5 Scenario B).
    _last_server_item_id: dict[str, str] = {}
    # Server item ids already loaded into runner history. Used to drop a late
    # forwarded copy of a message the recovery/catch-up path is already using.
    _loaded_server_item_ids: dict[str, set[str]] = {}
    # Per-session SSE event queue. proxy_stream and turn lifecycle
    # helpers put events here; GET /stream reads and removes them.
    # Events accumulate while no subscriber is reading, so tunnel
    # drops don't lose events — the relay drains on reconnect.
    _session_event_queues = _session_event_queues_ref
    # Per-session async inbox queues for sys_call_async /
    # sys_read_inbox (SESSION_REARCHITECTURE Step 7 partial).
    _session_inboxes = _session_inboxes_ref
    # Per-session background async tasks keyed by handle_id.
    # Each entry is (task, cancel_event) so cancellation is instant.
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    def _has_active_work() -> bool:
        """
        Return whether this runner is currently executing agent work.

        Used by the out-of-process runner's inactivity watchdog. The
        closure-local ``_active_turns`` catches turns owned directly by
        ``runner/app.py``; ``process_manager.has_active_turn`` catches
        in-flight responses tracked by the harness subprocess manager.

        :returns: ``True`` while any session has an active agent turn.
        """
        if _active_turns:
            return True
        if process_manager is None:
            return False
        session_ids = set(_session_start_cache) | set(_session_agent_ids)
        return any(process_manager.has_active_turn(session_id) for session_id in session_ids)

    app.state.has_active_work = _has_active_work

    def _remember_loaded_server_item_ids(session_id: str, items: list[dict[str, Any]]) -> None:
        """
        Record server item ids that have been loaded into runner history.

        :param session_id: Session/conversation identifier.
        :param items: Raw item objects returned by the Omnigent server.
        :returns: None.
        """
        ids = _loaded_server_item_ids.setdefault(session_id, set())
        for item in items:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                ids.add(item_id)

    def _active_turn_already_loaded_persisted_message(
        session_id: str,
        message_body: dict[str, Any],
    ) -> bool:
        """
        Return whether an active turn already loaded this persisted message.

        Server-side persist-before-forward can race with runner recovery:
        recovery/catch-up may start the turn from stored history, then the
        original forwarded POST arrives late. In that case buffering the late
        copy starts a duplicate continuation turn and strands sub-agent
        completion. A real new message has a new persisted item id and will not
        match this loaded-id set.
        """
        persisted_item_id = message_body.get("persisted_item_id")
        return (
            isinstance(persisted_item_id, str)
            and bool(persisted_item_id)
            and persisted_item_id in _loaded_server_item_ids.get(session_id, set())
        )

    def _ensure_session_coordination_state(
        session_id: str,
        *,
        agent_id: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """
        Create idempotent per-session coordination state.

        ``POST /v1/sessions`` normally initializes these maps, but a tunneled
        MCP tool call can race ahead of that handshake. Do not replace existing
        queues: they may already contain undrained async/sub-agent inbox
        payloads.
        """

        if agent_id is not None:
            _session_agent_ids[session_id] = agent_id
        if session_id not in _session_event_queues:
            _session_event_queues[session_id] = asyncio.Queue()
        if session_id not in _session_inboxes:
            _session_inboxes[session_id] = asyncio.Queue()
        if session_id not in _session_async_tasks:
            _session_async_tasks[session_id] = {}
        if sub_agent_name:
            _session_sub_agent_names[session_id] = sub_agent_name

    def _publish_event(session_id: str, event: dict[str, Any]) -> None:
        """Put an event on the session's queue for GET /stream.

        Creates the queue lazily if it doesn't exist — handles
        the case where a turn runs before POST /v1/sessions
        initializes session state (e.g. on resume when the
        tunnel connect callback fires before the runner client
        is ready).

        :param session_id: Session/conversation identifier.
        :param event: The SSE event dict to enqueue.
        """
        queue = _session_event_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            _session_event_queues[session_id] = queue
        queue.put_nowait(event)
        # Mirror a child sub-agent's status / preview deltas onto the
        # PARENT's stream. No-op for non-child sessions. Single chokepoint
        # so every session.status publish is covered.
        _fan_out_child_delta_to_parent(session_id, event)

    def _child_preview_from_status(
        session_id: str,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> str | None:
        """
        Return a child-session preview for an idle status edge.

        Native terminal status must pass AP-forwarded text and disable the
        history fallback because Omnigent owns native transcript persistence. The
        fallback remains for in-process harnesses whose assistant text is
        accumulated only in runner-local history.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param latest_assistant_text: Authoritative assistant text forwarded
            with an external status event, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether to read runner-local
            history when no explicit assistant text was provided.
        :returns: Truncated preview text, or ``None`` when there is no
            non-empty preview source.
        """
        if latest_assistant_text is not None:
            reply_source = latest_assistant_text
        elif allow_history_preview_fallback:
            reply_source = _extract_last_assistant_text(session_id)
        else:
            return None
        reply = reply_source.strip()
        if not reply:
            return None
        return _truncate_child_preview(reply)

    def _child_status_body(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        include_error: bool = False,
    ) -> dict[str, Any]:
        """
        Build the ``child`` object for a parent-stream status update.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param meta: Registered child-to-parent fan-out metadata.
        :param status: Child session status, e.g. ``"running"``.
        :param error: Failure detail from the ``session.status`` event.
        :param include_error: Whether to include ``last_task_error`` in the
            partial payload. ``True`` for failed edges and for activity edges
            that clear a stale failure.
        :returns: Child summary payload for ``session.child_session.updated``.
        """
        busy = status in ("running", "waiting")
        child = {
            "id": session_id,
            "title": meta.title,
            "tool": meta.tool,
            "session_name": meta.session_name,
            "busy": busy,
            "current_task_status": _session_status_to_task_status(status),
        }
        if include_error:
            child["last_task_error"] = error
        return child

    def _child_error_from_status_event(
        status: str | None,
        event: dict[str, Any],
    ) -> dict[str, str] | None:
        """
        Extract typed failure details from a generic ``session.status`` event.

        :param status: Status value from the event, e.g. ``"failed"``.
        :param event: Published status event.
        :returns: ``{"code": "...", "message": "..."}`` for failed events
            with a valid error payload, otherwise ``None``.
        """
        if status != "failed":
            return None
        raw_error = event.get("error")
        if not isinstance(raw_error, dict):
            return None
        raw_code = raw_error.get("code")
        raw_message = raw_error.get("message")
        if not isinstance(raw_code, str) or not isinstance(raw_message, str):
            return None
        if not raw_code or not raw_message:
            return None
        return {"code": raw_code, "message": raw_message}

    def _build_child_status_update(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> dict[str, Any] | None:
        """
        Build a parent-stream child update for one status edge.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param meta: Registered child-to-parent fan-out metadata.
        :param status: Child session status, e.g. ``"running"``.
        :param error: Failure detail from a failed ``session.status`` edge.
        :param latest_assistant_text: Explicit preview text, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether to read runner history.
        :returns: Update event, or ``None`` when busy/task status did not change.
        """
        if status in ("running", "waiting"):
            mark_subagent_work_started(session_id)
        busy = status in ("running", "waiting")
        task_status = _session_status_to_task_status(status)
        error_signature = (error["code"], error["message"]) if error is not None else None
        include_error = status in ("running", "waiting") or error is not None
        if (
            meta.last_busy == busy
            and meta.last_task_status == task_status
            and meta.last_error == error_signature
        ):
            return None
        meta.last_busy = busy
        meta.last_task_status = task_status
        meta.last_error = error_signature
        child = _child_status_body(
            session_id,
            meta,
            status,
            error=error,
            include_error=include_error,
        )
        if not busy:
            preview = _child_preview_from_status(
                session_id,
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if preview is not None:
                child["last_message_preview"] = preview
        return {
            "type": "session.child_session.updated",
            "conversation_id": meta.parent_id,
            "child_session_id": session_id,
            "child": child,
        }

    def _fan_out_child_delta_to_parent(
        session_id: str,
        event: dict[str, Any],
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> None:
        """Republish a child's status/preview delta onto its parent's stream.

        Used for both runner-published ``session.status`` events and synthetic
        native status projections. It coalesces busy-state edges and emits
        ``session.child_session.updated`` on the parent stream.

        :param session_id: Session the event was published for.
        :param event: Published or synthetic status event, e.g.
            ``{"type": "session.status", "status": "running"}``.
        :param latest_assistant_text: Authoritative assistant text from an
            external terminal status, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether an idle child update
            may read runner-local history when explicit text is missing.
        """
        meta = _child_session_parents.get(session_id)
        if meta is None:
            return
        evt_type = event.get("type")
        if evt_type == "session.status":
            raw_status = event.get("status")
            status = raw_status if isinstance(raw_status, str) else None
            child_update = _build_child_status_update(
                session_id,
                meta,
                status,
                error=_child_error_from_status_event(status, event),
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if child_update is not None:
                _publish_event(meta.parent_id, child_update)

    if resource_registry is None:
        resource_registry = SessionResourceRegistry(
            terminal_registry=terminal_registry,
            runner_workspace=runner_workspace,
            per_session_workspace=per_session_workspace,
        )
    app.state.session_resource_registry = resource_registry

    def _publish_terminal_activity(session_id: str, terminal_id: str) -> None:
        """Publish a transient terminal-activity pulse onto the session stream.

        Invoked on the event loop by the resource registry's per-terminal
        pane watcher when the pane produces output. The web turns this
        into the "active" badge for any terminal — no client PTY attach.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id, e.g.
            ``"terminal_zsh_s1"``.
        """
        _publish_event(
            session_id,
            {
                "type": "session.terminal.activity",
                "session_id": session_id,
                "terminal_id": terminal_id,
            },
        )

    resource_registry.set_terminal_activity_publisher(_publish_terminal_activity)

    def _publish_session_status(session_id: str, status: str) -> None:
        """Publish a PTY-activity-derived ``session.status`` edge.

        Invoked on the event loop by the resource registry's claude-native
        agent-terminal watcher when the pane crosses an activity/idle edge.
        Emitting the same ``session.status`` shape the runner uses for its
        own turns lets the Omnigent server relay it through the normal status
        path (cache + SSE). The watcher already dedupes to edges, so this
        only fires on a real running⇄idle transition.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param status: New working status, ``"running"`` or ``"idle"``.
        """
        _publish_event(
            session_id,
            {"type": "session.status", "status": status},
        )

    resource_registry.set_session_status_publisher(_publish_session_status)

    def _format_terminal_command_for_failure(event: TerminalExitEvent) -> str:
        """Format a launch command without exposing possibly secret argv."""
        if event.command is None:
            return "unknown"
        if event.args_count is None or event.args_count == 0:
            return event.command
        noun = "arg" if event.args_count == 1 else "args"
        return (
            f"{event.command} ({event.args_count} {noun}; "
            "argv omitted because terminal args may contain secrets)"
        )

    def _format_required_terminal_exit_output(event: TerminalExitEvent) -> str:
        """Build the sub-agent failure text for a required terminal exit."""
        command = _format_terminal_command_for_failure(event)
        cwd = event.cwd or "unknown"
        parts = [
            "Required terminal exited unexpectedly; the session runtime is no longer available.",
            "",
            "Terminal diagnostics:",
            f"terminal: {event.terminal_name}:{event.session_key}",
            f"command: {command}",
            f"cwd: {cwd}",
        ]
        if event.last_output:
            parts.extend(["", "Last captured terminal output:", event.last_output])
        else:
            parts.extend(
                [
                    "",
                    "Last captured terminal output: unavailable. The process exited before "
                    "Omnigent captured a pane snapshot.",
                ]
            )
        return "\n".join(parts)

    def _release_failed_required_terminal_session(session_id: str) -> None:
        """Release the harness subprocess for a session whose runtime died."""
        if process_manager is None:
            return

        async def _release() -> None:
            try:
                await process_manager.release(session_id)
            except Exception:
                _logger.exception(
                    "Failed to release harness subprocess after required terminal exit: "
                    "session=%s",
                    session_id,
                )

        task = asyncio.create_task(
            _release(),
            name=f"required-terminal-release:{session_id}",
        )
        task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(task)

    def _publish_terminal_exit(event: TerminalExitEvent) -> None:
        """Publish terminal-exit lifecycle effects from the resource registry."""
        _publish_event(
            event.session_id,
            {
                "type": "session.resource.deleted",
                "resource_id": event.terminal_id,
                "resource_type": "terminal",
                "session_id": event.session_id,
            },
        )
        if event.lifecycle != TerminalLifecycle.REQUIRED:
            return

        output = _format_required_terminal_exit_output(event)
        _publish_event(
            event.session_id,
            {
                "type": "session.status",
                "status": "failed",
                "error": {
                    "code": "required_terminal_exited",
                    "message": output,
                },
            },
        )
        _mark_subagent_terminal_and_wake(
            event.session_id,
            status="failed",
            output=output,
        )
        _release_failed_required_terminal_session(event.session_id)

    resource_registry.set_terminal_exit_publisher(_publish_terminal_exit)

    # The runner owns a filesystem registry when it has a local workspace
    # (the CLI workspace path). In practice runner_workspace is always set
    # for the real runner — the None branch exists only to keep the
    # signature flexible for tests and embedded use, but production code
    # never passes None here.
    # The registry is exposed on app.state so tests can seed it.
    from omnigent.runtime.filesystem_registry import (
        FilesystemRegistry,
        create_filesystem_registry,
    )

    if runner_workspace is not None:
        filesystem_registry = create_filesystem_registry(watch_path=runner_workspace)
    else:
        filesystem_registry = None
    app.state.filesystem_registry = filesystem_registry

    # Per-session filesystem registries for sessions whose workspace
    # differs from the runner's global workspace (e.g. git worktree
    # sessions). Keyed by session_id. The global filesystem_registry
    # is used when the session workspace matches runner_workspace.

    _session_fs_registries: dict[str, FilesystemRegistry] = {}

    async def _session_snapshot(session_id: str) -> _SessionSnapshot:
        """
        Fetch the session's server snapshot once, shared by all readers.

        Issues a single ``GET /v1/sessions/{id}`` and projects its body
        into a :class:`_SessionSnapshot` (``created_at`` / ``workspace`` /
        ``agent_id``). A per-session lock makes this single-flight: when a
        startup burst of consumers (registration, workspace resolution,
        spec resolution) calls concurrently, the first does the fetch and
        the rest read the cached result instead of issuing their own
        request.

        Only a *complete* snapshot — HTTP 200 with ``agent_id`` already
        bound — is memoized. A transient non-200, or a 200 whose
        ``agent_id`` is still null (the session exists but the agent has
        not bound yet), returns a fallback/partial snapshot without
        caching. This preserves retry-until-bound: spec resolution keeps
        refetching until the binding appears, instead of latching onto a
        stale ``agent_id=None`` and raising forever. Registration and
        workspace are unaffected — they memoize ``created_at`` /
        ``workspace`` in their own projection caches on first read.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The session snapshot. Always returns a value; failure
            is signaled via ``ok=False`` rather than raising, so
            best-effort callers can use the fallback fields directly.
        """
        cached = _session_snapshot_cache.get(session_id)
        if cached is not None:
            return cached
        lock = _session_snapshot_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Re-check under the lock: a concurrent caller may have
            # populated the cache while we waited to acquire it.
            cached = _session_snapshot_cache.get(session_id)
            if cached is not None:
                return cached
            status_code: int | None = None
            created_at: float | None = None
            workspace: str | None = None
            agent_id: str | None = None
            sub_agent_name: str | None = None
            try:
                resp = await server_client.get(f"/v1/sessions/{session_id}")
                status_code = resp.status_code
                if resp.status_code == 200:
                    # The GET /v1/sessions/{id} body — read defensively via
                    # ``.get`` (sweep-2 BDP-2366 names the projected subset).
                    body: SessionSnapshotPayload = resp.json()
                    raw_created = body.get("created_at")
                    if raw_created is not None:
                        created_at = float(raw_created)
                    workspace = body.get("workspace")
                    raw_agent_id = body.get("agent_id")
                    if isinstance(raw_agent_id, str) and raw_agent_id:
                        agent_id = raw_agent_id
                    # Sub-agent identity (SessionResponse.sub_agent_name).
                    # Projected here so harness resolution can swap to the
                    # child's sub-spec even after the in-memory
                    # _session_sub_agent_names map is lost (reconnect /
                    # cache eviction) — the bug that respawned a sub-agent's
                    # claude-native harness as the parent's claude-sdk and
                    # tore down its terminal ("Bridge closed").
                    raw_sub_agent = body.get("sub_agent_name")
                    if isinstance(raw_sub_agent, str) and raw_sub_agent:
                        sub_agent_name = raw_sub_agent
            except Exception:  # noqa: BLE001 — best-effort; created_at falls back to wall time
                pass
            snapshot = _SessionSnapshot(
                ok=status_code == 200,
                status_code=status_code,
                created_at=created_at if created_at is not None else time.time(),
                workspace=workspace,
                agent_id=agent_id,
                sub_agent_name=sub_agent_name,
            )
            # Cache only a complete snapshot. A 200 with agent_id still
            # null means the agent has not bound yet; caching it would
            # freeze spec resolution into raising NOT_FOUND forever, since
            # this cache never refreshes on server-side binding.
            # Cache only a complete snapshot. A 200 with agent_id still
            # null means the agent has not bound yet; caching it would
            # freeze spec resolution into raising NOT_FOUND forever, since
            # this cache never refreshes on server-side binding.
            if snapshot.ok and snapshot.agent_id is not None:
                _session_snapshot_cache[session_id] = snapshot
            return snapshot

    async def _session_workspace_value(session_id: str) -> str | None:
        """
        Lazily resolve + cache the session's server-stored workspace path.

        The agent executes in this directory on this runner (the
        claude-native TUI's cwd, the in-process harness workspace, a git
        worktree, ...). The ``POST /v1/sessions`` body omits ``workspace``,
        so the runner asks the server. Reads from the shared
        :func:`_session_snapshot` so it does not issue its own
        ``GET /v1/sessions/{id}``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The raw workspace string (an absolute path on this
            runner), or ``None`` when the session has no explicit
            workspace or the lookup fails.
        """
        if session_id not in _session_workspace_cache:
            snapshot = await _session_snapshot(session_id)
            _session_workspace_cache[session_id] = snapshot.workspace
        return _session_workspace_cache.get(session_id)

    async def _resolve_session_fs_registry(
        session_id: str,
    ) -> FilesystemRegistry | None:
        """Return the filesystem registry for *session_id*.

        For sessions whose server-stored workspace matches the runner's
        global ``runner_workspace`` (the common case), returns the
        shared ``filesystem_registry``.  For sessions with a different
        workspace (e.g. git worktree sessions), creates and caches a
        per-session registry rooted at the session's workspace.

        Lazily fetches the session workspace from the server on first
        call (the ``POST /v1/sessions`` body does not include
        ``workspace``, so the runner must ask the server).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The appropriate :class:`FilesystemRegistry`, or
            ``None`` when no registry can be created.
        """
        if session_id in _session_fs_registries:
            return _session_fs_registries[session_id]

        session_workspace = await _session_workspace_value(session_id)
        if session_workspace is None:
            return filesystem_registry

        session_ws_path = Path(session_workspace).resolve()
        runner_ws_resolved = runner_workspace.resolve() if runner_workspace is not None else None
        if runner_ws_resolved is not None and session_ws_path == runner_ws_resolved:
            return filesystem_registry

        registry = create_filesystem_registry(watch_path=session_ws_path)
        _session_fs_registries[session_id] = registry
        return registry

    from omnigent.entities.environment_filesystem import (
        FilesystemEntry,
        ResourceError,
    )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """
        Translate application errors to structured JSON responses.

        :param request: The incoming request.
        :param exc: The application error.
        :returns: JSON error response with the mapped HTTP status.
        """
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ValueError)
    async def _handle_value_error(
        request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        """Translate ValueErrors (e.g. from resolve_environment).

        :param request: The incoming request.
        :param exc: The value error.
        :returns: 400 JSON error response.
        """
        del request
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": str(exc),
                },
            },
        )

    @app.exception_handler(ResourceError)
    async def _handle_resource_error(
        request: Request,
        exc: ResourceError,
    ) -> JSONResponse:
        """Translate ResourceError subclasses to HTTP responses.

        :param request: The incoming request.
        :param exc: The resource error.
        :returns: JSON error response with appropriate status code.
        """
        del request
        from omnigent.entities.environment_filesystem import (
            DirectoryNotEmpty,
            FilesystemPathNotFound,
            FileTooLarge,
            InvalidPath,
            UnsupportedMediaType,
        )

        status = 500
        if isinstance(exc, FilesystemPathNotFound):
            status = 404
        elif isinstance(exc, InvalidPath):
            status = 400
        elif isinstance(exc, DirectoryNotEmpty):
            status = 409
        elif isinstance(exc, FileTooLarge):
            status = 413
        elif isinstance(exc, UnsupportedMediaType):
            status = 415
        return JSONResponse(
            status_code=status,
            content={
                "error": {"code": exc.code, "message": exc.message},
            },
        )



def _exec_runner_route_chunks(ns: dict) -> None:
    """Execute route registration chunks in a shared closure namespace."""
    import importlib.resources as _res
    from pathlib import Path as _Path
    _routes_pkg = _Path(__file__).resolve().parent / "routes"
    for _fname in (
        "health.py",
        "create_session.py",
        "stream_session.py",
        "get_session.py",
        "delete_session.py",
        "post_session_events.py",
        "list_session_resources.py",
        "list_session_environments.py",
        "get_session_environment.py",
        "list_session_terminals.py",
        "create_session_terminal.py",
        "get_session_terminal.py",
        "transfer_session_terminal.py",
        "delete_session_terminal.py",
        "terminal_resource_attach_ws.py",
        "list_environment_root.py",
        "search_environment_files.py",
        "list_filesystem_changes.py",
        "read_environment_file_diff.py",
        "read_or_list_environment_path.py",
        "write_environment_file.py",
        "edit_environment_file.py",
        "delete_environment_path.py",
        "get_session_skills.py",
        "resolve_session_skill.py",
        "run_environment_shell.py",
        "get_session_resource.py",
        "cleanup_session_resources.py",
        "reset_session_state.py",
        "mcp_execute.py",
        "summarize.py",
        "elicitation.py",
    ):
        _code = (_routes_pkg / _fname).read_text()
        exec(compile(_code, str(_routes_pkg / _fname), "exec"), ns)
    _exec_runner_route_chunks(locals().copy())

    async def _catch_up_scan() -> None:
        """Catch-up scan after tunnel reconnect (Step 8.5 Scenario B).

        For each session with in-memory history, query the server
        for items after the last known item. Append new items to
        history and start a turn if idle and new user messages
        arrived.
        """
        for session_id in list(_session_histories):
            if _is_native_harness(session_id):
                # Same rule as session-start recovery: do not synthesize
                # catch-up turns by replaying mirrored native transcript items.
                continue
            try:
                # Paginate from the last known cursor until all
                # missed items are fetched.
                after_id = _last_server_item_id.get(session_id)
                all_new: list[dict[str, Any]] = []
                while True:
                    params: dict[str, str] = {
                        "limit": "100",
                        "order": "asc",
                    }
                    if after_id:
                        params["after"] = after_id
                    resp = await server_client.get(
                        f"/v1/sessions/{session_id}/items",
                        params=params,
                        timeout=10.0,
                    )
                    if resp.status_code != 200:
                        break
                    page = resp.json()
                    page_items = page.get("data", [])
                    if not page_items:
                        break
                    _remember_loaded_server_item_ids(session_id, page_items)
                    all_new.extend(page_items)
                    last_id = page_items[-1].get("id")
                    if last_id:
                        after_id = last_id
                        _last_server_item_id[session_id] = last_id
                    if not page.get("has_more", False):
                        break
                if not all_new:
                    continue
                new_items = _convert_raw_items_to_input(all_new)
                _session_histories.setdefault(session_id, []).extend(
                    new_items,
                )
                # Start a turn if idle and new user messages arrived.
                if (
                    session_id not in _active_turns
                    and new_items
                    and new_items[-1].get("role") == "user"
                ):
                    _active_turns[session_id] = None
                    _publish_turn_status(session_id, "running")
                    agent_id = _session_agent_ids.get(session_id)
                    msg_body = {
                        "agent_id": agent_id,
                        "model": agent_id or "",
                    }
                    _turn_task = asyncio.create_task(
                        _run_turn_bg(msg_body, session_id),
                        name=f"turn-catchup-{session_id}",
                    )
                    _active_turns[session_id] = _turn_task
                    _turn_task.add_done_callback(
                        _background_tasks.discard,
                    )
                    _background_tasks.add(_turn_task)
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Catch-up scan failed for %s",
                    session_id,
                    exc_info=True,
                )

    # Expose catch-up scan so _entry.py can wire it as on_reconnect.
    app.state.catch_up_scan = _catch_up_scan

    return app
