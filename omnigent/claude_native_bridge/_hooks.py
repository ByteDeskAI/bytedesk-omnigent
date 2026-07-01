"""Bridge utilities for the native Claude Code wrapper.

The native wrapper has two live processes that need to rendezvous:

- Claude Code, running in the user's terminal resource.
- The Omnigent harness turn, running when the web UI submits a
  message to the session agent.

This module owns the small filesystem rendezvous directory plus two
helper surfaces:

- An MCP stdio server (``serve-mcp`` subcommand) that Claude Code
  launches as a child process. It advertises Omnigent tools to
  Claude (workspace ``sys_os_*`` tools outside an active turn,
  active-turn Omnigent tools via a per-turn relay).
- A tmux send-keys path. Web UI messages are delivered to Claude by
  typing them into the same tmux pane the user is attached to;
  Claude treats them as ordinary user input. The runner advertises
  the pane's socket + target in ``tmux.json`` after launching the
  ``claude/main`` terminal.

Claude's experimental Channels MCP capability was the original input
path but is blocked at the org policy layer, so this bridge does not
use it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error, request

from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing

from omnigent.inner.bundle_skills import claude_native_skill_args
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import OSEnvironment, create_os_environment
from omnigent.reasoning_effort import CLAUDE_EFFORTS
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.os_env import build_os_env_tools

BRIDGE_DIR_ENV_VAR = "HARNESS_CLAUDE_NATIVE_BRIDGE_DIR"
REQUEST_SESSION_ID_ENV_VAR = "HARNESS_CLAUDE_NATIVE_REQUEST_SESSION_ID"
BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"

# Root for the per-process Claude bridge tree. Namespaced by uid so
# other Unix users on the same host cannot read the bearer token or
# pre-create the parent as a symlink to redirect the bridge tree. The
# trusted parent (`/tmp`) is shared; everything under
# `_BRIDGE_ROOT_PARENT` must be owned by the current uid and not be a
# symlink — see :func:`_ensure_secure_dir`.
_TRUSTED_PARENT = Path("/tmp")
_BRIDGE_ROOT_PARENT = _TRUSTED_PARENT / f"omnigent-{os.getuid()}"
_BRIDGE_ROOT = _BRIDGE_ROOT_PARENT / "claude-native"
_CONFIG_FILE = "bridge.json"
_SERVER_FILE = "server.json"
_STATE_FILE = "state.json"
_HOOKS_FILE = "hooks.jsonl"
_RECENT_LOCAL_COMMAND_LINE_LIMIT = 200
_RECENT_LOCAL_COMMAND_WINDOW_S = 10.0
_FORKED_FROM_LINE_LIMIT = 200
_TOOL_RELAY_FILE = "tool_relay.json"
_TMUX_FILE = "tmux.json"
_PERMISSION_HOOK_FILE = "permission_hook.json"
_CONTEXT_FILE = "context.json"
_USER_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MCP_SERVER_NAME = "omnigent"
_MCP_PROTOCOL_VERSION = "2024-11-05"
# Tools-changed: harness POSTs to the bridge MCP server's localhost
# control endpoint, which emits ``notifications/tools/list_changed``
# on its MCP stdout. Standard MCP notification — unrelated to the
# experimental Claude Channels feature that this module no longer
# uses.
_TOOLS_CHANGED_READY_TIMEOUT_S = 30.0
_TOOLS_CHANGED_POST_TIMEOUT_S = 10.0
# Ceiling the relay HTTP handler (``_run_relay_tool``) waits for a single
# tool dispatch to complete on the harness event loop.
_TOOL_CALL_TIMEOUT_S = 300.0
# Timeout for the bridge's POST to the active-turn relay server
# (``_call_relay_tool``). This is the OUTER hop: it waits for the relay
# handler's entire ``_TOOL_CALL_TIMEOUT_S`` dispatch, which itself fans out
# to the Omnigent policy server and back. It MUST exceed ``_TOOL_CALL_TIMEOUT_S``
# so the inner handler times out first and returns a clean MCP error over
# HTTP 200 — rather than the outer ``urlopen`` raising and tearing down the
# stdio MCP server (see ``_stdio_jsonrpc_loop``). The previous flat 10s sat
# below the real round-trip latency under load, so slow-but-healthy calls
# (session history reads, shell) tripped it and crashed the bridge.
_TOOL_RELAY_POST_TIMEOUT_S = _TOOL_CALL_TIMEOUT_S + 30.0
# Web-UI → Claude input now flows through tmux send-keys, not
# Claude's experimental Channels MCP capability. The runner writes
# ``tmux.json`` after the Claude terminal launches; the harness
# tails it and shells out to tmux.
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 5.0
# Claude Code renders this prompt glyph in its input box once the TUI
# is interactive. We poll ``capture-pane`` for it before injecting the
# first message so keystrokes typed during Claude's boot aren't dropped.
# The glyph persists while Claude is busy responding, so its presence
# means "input box mounted" (not "idle"), which is what injection needs.
_CLAUDE_PROMPT_GLYPH = "❯"
# How many trailing non-empty lines to scan for the prompt glyph. The
# input box sits near the bottom of the pane; scanning only the tail
# avoids false positives from the glyph appearing in scrollback output.
# The window has to clear the footer rendered below the box — some
# people's statuslines run ~3 lines — so the ``❯`` row isn't the last
# non-empty line.
_PROMPT_SCAN_TAIL_LINES = 5
_CLAUDE_READY_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1  # let the TUI commit a paste before the separate submit Enter
# How long to wait for the pasted draft to visibly land in Claude's
# input box before sending the submit Enter. Claude Code coalesces
# rapid stdin bursts into a paste, so an Enter sent while the TUI is
# still consuming the paste gets folded in as a newline instead of
# submitting — the draft then sits unsent. Polling for the draft makes
# the handoff deterministic where the old fixed sleep raced it.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# After the submit Enter, how long to keep checking that the draft
# actually left the input box (re-sending Enter while it hasn't)
# before failing loud.
_SUBMIT_VERIFY_TIMEOUT_S = 10.0
# Minimum spacing between repeated submit Enters during verification.
# Long enough for the TUI to clear the box after a successful submit
# (so a slow-but-successful first Enter isn't double-tapped), short
# enough that a swallowed Enter is retried promptly.
_SUBMIT_RETRY_INTERVAL_S = 1.0
# Claude Code collapses large pastes into this placeholder in the
# input box instead of rendering the text itself.
_PASTED_PLACEHOLDER_PREFIX = "[Pasted text"
# How many characters of the draft's first line to use when checking
# whether the draft is rendered in the input box. Short enough to fit
# on the prompt row of a default 80-column detached pane.
_DRAFT_NEEDLE_MAX_CHARS = 24

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def record_hook_event(bridge_dir: Path, payload: dict[str, Any]) -> None:
    """
    Record one Claude Code hook payload in the bridge directory.

    :param bridge_dir: Bridge directory path.
    :param payload: Hook JSON object read from Claude Code stdin,
        e.g. ``{"hook_event_name": "Stop", "transcript_path":
        "/home/user/.claude/projects/x/session.jsonl"}``.
    :returns: None.
    """
    _ensure_secure_dir(bridge_dir)
    envelope = {
        "recorded_at": time.time(),
        "payload": payload,
    }
    with (bridge_dir / _HOOKS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, separators=(",", ":")) + "\n")

    state = _read_json_file(bridge_dir / _STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    event_name = payload.get("hook_event_name")
    if isinstance(event_name, str) and event_name:
        state["last_hook_event_name"] = event_name
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        state["transcript_path"] = transcript_path
    claude_session_id = payload.get("session_id")
    if isinstance(claude_session_id, str) and claude_session_id:
        state["claude_session_id"] = claude_session_id
        seen = read_seen_claude_session_ids(bridge_dir)
        seen.add(claude_session_id)
        state["seen_claude_session_ids"] = sorted(seen)
    state["updated_at"] = time.time()
    _write_json_file(bridge_dir / _STATE_FILE, state)

def count_hook_events(bridge_dir: Path) -> int:
    """
    Count hook records currently written for a bridge.

    :param bridge_dir: Bridge directory path.
    :returns: Number of hook JSONL records.
    """
    path = bridge_dir / _HOOKS_FILE
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except FileNotFoundError:
        return 0

def read_hook_events_since(
    bridge_dir: Path,
    start_event_count: int,
) -> tuple[int, list[str]]:
    """
    Read hook event names appended after a hook cursor.

    The transcript forwarder uses this to publish ``session.status``
    events to Omnigent when Claude Code's ``Stop`` / ``StopFailure`` hooks
    fire — those are the only edges the wrapper can observe between
    Claude becoming idle and the JSONL transcript reflecting it.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: One-based cursor; lines at or before
        this count are skipped.
    :returns: ``(new_cursor, hook_event_names)`` — ``new_cursor`` is
        the line count after the read, suitable for the next call.
        Malformed lines are skipped silently but still advance the
        cursor so they are not retried indefinitely.
    """
    result = read_hook_events_since_with_position(bridge_dir, start_event_count)
    names = [record.event_name for record in result.records if record.event_name is not None]
    return result.event_cursor, names

def read_hook_events_since_with_position(
    bridge_dir: Path,
    start_event_count: int,
) -> HookReadResult:
    """
    Read hook records from a line cursor and return byte position.

    This compatibility reader supports existing durable state that
    only stored a hook line cursor. It scans once, returns complete
    records after ``start_event_count``, and reports the byte offset
    so future polls can seek directly.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: One-based cursor; lines at or before
        this count are skipped.
    :returns: Complete hook records plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        bridge_dir / _HOOKS_FILE,
        byte_offset=0,
        start_line=0,
        emit_after_line=start_event_count,
    )
    records = [_hook_record_from_jsonl_record(record) for record in read_result.records]
    return HookReadResult(
        event_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        records=records,
    )

def read_hook_events_from_offset(
    bridge_dir: Path,
    byte_offset: int,
    *,
    start_event_count: int,
) -> HookReadResult:
    """
    Read hook records appended after a byte offset.

    Only complete newline-terminated JSONL records are returned. A
    partial trailing hook record leaves the byte offset unchanged so
    the next poll retries it after Claude finishes the write.

    :param bridge_dir: Bridge directory path.
    :param byte_offset: Byte offset already consumed, e.g. ``1024``.
    :param start_event_count: Count of complete hook records already
        consumed. Used to keep the legacy cursor monotonic while byte
        offsets drive the actual seek.
    :returns: Complete hook records plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        bridge_dir / _HOOKS_FILE,
        byte_offset=byte_offset,
        start_line=start_event_count,
    )
    records = [_hook_record_from_jsonl_record(record) for record in read_result.records]
    return HookReadResult(
        event_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        records=records,
    )

def stop_hook_seen_since(bridge_dir: Path, start_event_count: int) -> bool:
    """
    Return whether Claude reported a stop event after a hook cursor.

    Only counts stop events from the parent Claude process — subagent
    stop events (whose ``transcript_path`` contains a ``subagents/``
    component) are ignored so a finishing subagent does not
    prematurely signal the parent turn as complete.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: Hook record count captured before a
        message is injected into the Claude terminal.
    :returns: ``True`` once a parent-process ``Stop`` or
        ``StopFailure`` hook has been recorded after the cursor.
    """
    path = bridge_dir / _HOOKS_FILE
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle, start=1):
                if index <= start_event_count:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = envelope.get("payload") if isinstance(envelope, dict) else None
                event_name = payload.get("hook_event_name") if isinstance(payload, dict) else None
                if event_name in {"Stop", "StopFailure"}:
                    transcript_path = (
                        payload.get("transcript_path") if isinstance(payload, dict) else None
                    )
                    if isinstance(transcript_path, str) and "/subagents/" in transcript_path:
                        continue
                    return True
    except FileNotFoundError:
        return False
    return False

def _hook_record_from_jsonl_record(record: _JsonlRecord) -> ClaudeHookRecord:
    """
    Convert one complete hook JSONL line into a hook record.

    :param record: Complete JSONL record read from ``hooks.jsonl``.
    :returns: Hook record with an event name when present. Malformed
        complete lines return ``event_name=None`` so callers can
        still advance durable cursors past them.
    """
    event_name: str | None = None
    try:
        envelope = json.loads(record.text) if record.text is not None else None
    except json.JSONDecodeError:
        envelope = None
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    raw_event_name = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if isinstance(raw_event_name, str) and raw_event_name:
        event_name = raw_event_name
    raw_source = payload.get("source") if isinstance(payload, dict) else None
    raw_recorded_at = envelope.get("recorded_at") if isinstance(envelope, dict) else None
    raw_claude_session_id = payload.get("session_id") if isinstance(payload, dict) else None
    raw_transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
    raw_previous_claude_session_id = (
        payload.get("omnigent_previous_claude_session_id") if isinstance(payload, dict) else None
    )
    raw_claude_session_was_seen = (
        payload.get("omnigent_claude_session_was_seen") if isinstance(payload, dict) else None
    )
    raw_clear_rotated_to = (
        payload.get("omnigent_clear_rotated_to") if isinstance(payload, dict) else None
    )
    raw_fork_detected = (
        payload.get("omnigent_fork_detected") if isinstance(payload, dict) else None
    )
    raw_fork_rotated_to = (
        payload.get("omnigent_fork_rotated_to") if isinstance(payload, dict) else None
    )
    # Extract todos from PostToolUse/TodoWrite hook payloads. Claude Code
    # fires this hook after every TodoWrite call with ``tool_input.todos``
    # containing the updated list. Other PostToolUse events have no todos.
    todos: list[dict[str, Any]] | None = None
    task_id: str | None = None
    task_subject: str | None = None
    task_status: str | None = None
    if event_name == "PostToolUse" and isinstance(payload, dict):
        raw_tool_name = payload.get("tool_name")
        if raw_tool_name == "TodoWrite":
            raw_tool_input = payload.get("tool_input")
            if isinstance(raw_tool_input, dict):
                raw_todos = raw_tool_input.get("todos")
                if isinstance(raw_todos, list):
                    todos = [t for t in raw_todos if isinstance(t, dict)]
        elif raw_tool_name == "TaskUpdate":
            raw_tool_input = payload.get("tool_input")
            if isinstance(raw_tool_input, dict):
                raw_task_id = raw_tool_input.get("taskId")
                raw_task_status = raw_tool_input.get("status")
                if isinstance(raw_task_id, str) and raw_task_id:
                    task_id = raw_task_id
                if isinstance(raw_task_status, str) and raw_task_status:
                    task_status = raw_task_status
    elif event_name == "TaskCreated" and isinstance(payload, dict):
        raw_task_id = payload.get("task_id")
        raw_task_subject = payload.get("task_subject")
        if isinstance(raw_task_id, str) and raw_task_id:
            task_id = raw_task_id
        if isinstance(raw_task_subject, str) and raw_task_subject:
            task_subject = raw_task_subject
        task_status = "pending"
    elif event_name == "TaskCompleted" and isinstance(payload, dict):
        raw_task_id = payload.get("task_id")
        if isinstance(raw_task_id, str) and raw_task_id:
            task_id = raw_task_id
        task_status = "completed"
    return ClaudeHookRecord(
        event_cursor=record.line_number,
        byte_offset=record.next_byte_offset,
        event_name=event_name,
        recorded_at=raw_recorded_at
        if isinstance(raw_recorded_at, (int, float)) and not isinstance(raw_recorded_at, bool)
        else None,
        source=raw_source if isinstance(raw_source, str) and raw_source else None,
        claude_session_id=(
            raw_claude_session_id
            if isinstance(raw_claude_session_id, str) and raw_claude_session_id
            else None
        ),
        transcript_path=(
            Path(raw_transcript_path)
            if isinstance(raw_transcript_path, str) and raw_transcript_path
            else None
        ),
        previous_claude_session_id=(
            raw_previous_claude_session_id
            if isinstance(raw_previous_claude_session_id, str) and raw_previous_claude_session_id
            else None
        ),
        claude_session_was_seen=(
            raw_claude_session_was_seen if isinstance(raw_claude_session_was_seen, bool) else None
        ),
        clear_rotated_to=(
            raw_clear_rotated_to
            if isinstance(raw_clear_rotated_to, str) and raw_clear_rotated_to
            else None
        ),
        fork_detected=raw_fork_detected is True,
        fork_rotated_to=(
            raw_fork_rotated_to
            if isinstance(raw_fork_rotated_to, str) and raw_fork_rotated_to
            else None
        ),
        todos=todos,
        task_id=task_id,
        task_subject=task_subject,
        task_status=task_status,
    )

def _read_complete_jsonl_records(
    path: Path,
    *,
    byte_offset: int,
    start_line: int,
    emit_after_line: int | None = None,
) -> _JsonlReadResult:
    """
    Read complete newline-terminated records from a JSONL file.

    The reader seeks to ``byte_offset`` and stops before a trailing
    partial line. That partial line's bytes are retried by the next
    poll after the writer appends its newline.

    :param path: JSONL file path.
    :param byte_offset: Byte offset where reading should begin,
        e.g. ``4096``.
    :param start_line: Count of complete records before
        ``byte_offset``, e.g. ``12``.
    :param emit_after_line: When provided, complete records at or
        before this line number are counted for cursor migration but
        not decoded or stored.
    :returns: Complete records plus updated line and byte cursors.
    """
    if byte_offset < 0:
        raise ValueError(f"byte_offset must be non-negative, got {byte_offset}")
    if start_line < 0:
        raise ValueError(f"start_line must be non-negative, got {start_line}")
    records: list[_JsonlRecord] = []
    cursor = start_line
    position = byte_offset
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            if byte_offset > file_size:
                handle.seek(0)
                cursor = 0
                position = 0
            else:
                handle.seek(byte_offset)
            while True:
                record_start = position
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                position = handle.tell()
                cursor += 1
                if emit_after_line is not None and cursor <= emit_after_line:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                records.append(
                    _JsonlRecord(
                        line_number=cursor,
                        byte_offset=record_start,
                        next_byte_offset=position,
                        text=text,
                    )
                )
    except FileNotFoundError:
        return _JsonlReadResult(
            line_cursor=start_line,
            byte_offset=byte_offset,
            records=[],
        )
    return _JsonlReadResult(
        line_cursor=cursor,
        byte_offset=position,
        records=records,
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
    for _key, _value in _sib_args.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_bridge_io.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_cost.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_inject.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tmux.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_convert.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_read.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
