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
def _import_parent_bindings() -> None:
    from .. import (
        _constants as _parent_constants,
        _dispatch as _parent_dispatch,
        _forwarders as _parent_forwarders,
        _harness as _parent_harness,
        _helpers as _parent_helpers,
        _policy as _parent_policy,
        _state as _parent_state,
        _streaming as _parent_streaming,
        _subagents as _parent_subagents,
        _timers as _parent_timers,
        _tools as _parent_tools,
    )

    g = globals()
    for _mod in (
        _parent_constants,
        _parent_state,
        _parent_dispatch,
        _parent_forwarders,
        _parent_harness,
        _parent_helpers,
        _parent_policy,
        _parent_streaming,
        _parent_subagents,
        _parent_timers,
        _parent_tools,
    ):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()

def _publish_tmux_target_for_bridge(
    *,
    resource_registry: SessionResourceRegistry,
    session_id: str,
    bridge_id: str,
    terminal_name: str,
    session_key: str,
) -> None:
    """
    Advertise a launched terminal's tmux target to a bridge directory.

    Called from the terminal-launch POST when the caller opts in via
    truthy ``bridge_inject_dir`` in the body. The destination path is
    derived from a server-side bridge id, so a caller can't redirect
    the write.

    The ``claude-native`` harness reads ``tmux.json`` from the derived
    directory and shells out to ``tmux -S <socket> send-keys``. No-op
    if the registry has no live instance for the triple.

    :param resource_registry: Session resource registry that exposes
        the underlying terminal registry.
    :param session_id: Owning session/conversation id.
    :param bridge_id: Opaque bridge id from the session label, e.g.
        ``"bridge_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"claude"``.
    :param session_key: Session key, e.g. ``"main"``.
    :returns: None.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return
    instance = terminal_registry.get(session_id, terminal_name, session_key)
    if instance is None or not instance.running:
        return
    # Imported here to avoid pulling Claude-native specifics into the
    # generic runner module's import-time graph.
    from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, write_tmux_target

    write_tmux_target(
        bridge_dir_for_bridge_id(bridge_id),
        socket_path=instance.socket_path,
        tmux_target=instance.tmux_target,
    )

def _terminal_lookup_miss_reason(
    resource_registry: SessionResourceRegistry,
    session_id: str,
    terminal_id: str,
) -> str:
    """
    Explain why a terminal resource lookup returned ``None``.

    Used only for runner diagnostics after
    :meth:`SessionResourceRegistry.get_terminal_resource` has already
    performed the authoritative lookup and tmux liveness probe. The helper
    inspects in-memory registry state without running another tmux command,
    so the log line distinguishes absent resources from terminals that were
    registered but are now marked stopped.

    :param resource_registry: Runner resource registry for the session.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :returns: Short reason string for logs.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return "terminal_registry_missing"
    entries = terminal_registry.list_for_conversation(session_id)
    if not entries:
        return "session_has_no_registered_terminals"
    registered_ids = [
        terminal_resource_id(entry.terminal_name, entry.session_key) for entry in entries
    ]
    for entry in entries:
        if terminal_resource_id(entry.terminal_name, entry.session_key) != terminal_id:
            continue
        if not entry.instance.running:
            return (
                "terminal_registered_but_not_running "
                f"name={entry.terminal_name!r} session_key={entry.session_key!r} "
                f"socket={entry.instance.socket_path}"
            )
        return (
            "terminal_registered_but_liveness_probe_failed "
            f"name={entry.terminal_name!r} session_key={entry.session_key!r} "
            f"socket={entry.instance.socket_path}"
        )
    return f"terminal_id_not_registered registered_ids={registered_ids!r}"

def _log_terminal_lookup_miss(
    resource_registry: SessionResourceRegistry,
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Log a throttled terminal lookup miss diagnostic.

    Claude/Codex wrapper clients poll terminal GET endpoints while a runner
    starts. Without throttling, an INFO log per poll would flood the runner
    log for the full startup timeout. This emits immediately for each new
    reason and then at most once per interval while the reason persists.

    :param resource_registry: Runner resource registry for the session.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :returns: None.
    """
    reason = _terminal_lookup_miss_reason(resource_registry, session_id, terminal_id)
    now = time.monotonic()
    key = (session_id, terminal_id, reason)
    last = _terminal_lookup_miss_log_state.get(key)
    if last is not None and now - last < _TERMINAL_LOOKUP_MISS_LOG_INTERVAL_S:
        return
    _terminal_lookup_miss_log_state[key] = now
    _logger.info(
        "Terminal resource lookup miss: session=%s terminal_id=%s reason=%s",
        session_id,
        terminal_id,
        reason,
    )

def _publish_terminal_pending(
    publish_event: Callable[[str, dict[str, Any]], None],
    session_id: str,
    pending: bool,
) -> None:
    """
    Publish a terminal spin-up status event onto the session stream.

    Emitted by the auto-create path so the web UI can show a spinner on
    the Terminal pill while the runner boots a terminal-first session's
    terminal, and clear it once the terminal lands or auto-create
    fails. The Omnigent relay caches the latest value and republishes it, and
    seeds the ``terminal_pending`` snapshot field, so a client that
    connects mid-spin-up still sees the spinner. ``pending=False`` is
    what distinguishes "still starting up" from "no terminal" (killed /
    never created): once cleared, the client relies purely on whether a
    terminal resource exists.

    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` when a terminal is being created (show the
        spinner); ``False`` to clear it (terminal landed, or
        auto-create raised).
    """
    publish_event(
        session_id,
        {"type": "session.terminal_pending", "pending": pending},
    )

def _native_terminal_start_error_payload(exc: BaseException, runtime_name: str) -> dict[str, str]:
    """
    Build the structured error payload for a native terminal start failure.

    :param exc: Exception raised by the native terminal creation path,
        e.g. ``ImportError("Native Codex requires the 'codex' CLI on PATH.")``.
    :param runtime_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: ``{"code": ..., "message": ...}`` payload for SSE and
        JSON error responses. The message is a fixed, client-safe string;
        the raw cause is logged for operators, not surfaced to the caller.
    """
    _logger.warning("Native %s terminal start failed: %s", runtime_name, exc, exc_info=True)
    message = f"Native {runtime_name} terminal failed to start; see runner logs for details."
    return {"code": _NATIVE_TERMINAL_START_FAILED_CODE, "message": message}

def _publish_native_terminal_start_error(
    publish_event: Callable[[str, dict[str, Any]], None],
    session_id: str,
    runtime_name: str,
    exc: BaseException,
) -> dict[str, str]:
    """
    Publish live failure events for a native terminal start failure.

    The runner stays alive: the affected session receives
    ``session.status: failed`` with the structured cause, while resource
    panels and the relay keep working. The runner does not publish a
    bare ``response.error`` here because terminal auto-create happens
    outside a transcript turn; Omnigent writes and publishes the turn-scoped
    ``response.error`` only when it consumes a user message that cannot
    run because the terminal is failed.

    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runtime_name: Human-readable runtime name, e.g. ``"Claude"``.
    :param exc: The startup exception whose text should be surfaced.
    :returns: The structured error payload that was published on the
        status event.
    """
    error = _native_terminal_start_error_payload(exc, runtime_name)
    publish_event(
        session_id,
        {
            "type": "session.status",
            "status": "failed",
            "error": error,
        },
    )
    return error

def _native_terminal_start_error_response(exc: BaseException, runtime_name: str) -> JSONResponse:
    """
    Return a structured JSON error for native terminal ensure failures.

    :param exc: Exception raised by terminal auto-create.
    :param runtime_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: HTTP 500 response with an ``error`` object carrying the
        real failure message.
    """
    return JSONResponse(
        status_code=500,
        content={"error": _native_terminal_start_error_payload(exc, runtime_name)},
    )

def mark_subagent_work_terminal(
    child_session_id: str,
    *,
    status: TerminalStatus,
    output: str | None,
) -> _SubagentDeliveryAck:
    """
    Mark a sub-agent dispatch terminal and notify the parent inbox.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param status: Terminal status — one of
        :data:`SubagentWorkStatus.COMPLETED`,
        :data:`SubagentWorkStatus.FAILED`, or
        :data:`SubagentWorkStatus.CANCELLED`. A bare wire string
        (``"completed"`` / ``"failed"`` / ``"cancelled"``) is accepted and
        coerced; the ``TerminalStatus`` annotation makes a non-terminal value
        a type error at call sites.
    :param output: Child output or error text. ``None`` means the
        completion had no assistant text to deliver.
        If an earlier terminal report could not be delivered, a later
        report for the same child replaces the undelivered status and
        output before retrying parent inbox delivery.
    :returns: Delivery acknowledgement for this terminal report.
    :raises ValueError: If ``status`` is not a terminal status.
    """
    # Coerce wire strings to the enum and keep a runtime terminal guard for
    # safety, even though ``TerminalStatus`` enforces the subset at type level.
    status = SubagentWorkStatus(status)
    if status not in _SUBAGENT_TERMINAL_STATUSES:
        raise ValueError(
            f"sub-agent terminal status must be one of "
            f"{sorted(_SUBAGENT_TERMINAL_STATUSES)}; got {status!r}"
        )
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        if child_session_id in _drained_delivered_subagent_children:
            return _SubagentDeliveryAck(
                entry=None,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        return _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        )
    if entry.status in _SUBAGENT_TERMINAL_STATUSES:
        if entry.delivered:
            return _SubagentDeliveryAck(
                entry=entry,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        entry.status = status
        entry.output = output
        entry.completed_at = time.time()
        return _deliver_subagent_completion(entry)
    entry.status = status
    entry.output = output
    entry.completed_at = time.time()
    return _deliver_subagent_completion(entry)

