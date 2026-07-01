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

@dataclass(frozen=True)
class ClaudeTranscriptItem:
    """
    One Omnigent conversation item parsed from Claude's JSONL log.

    :param source_id: Stable idempotency key derived from the Claude
        transcript record UUID and content block position, e.g.
        ``"747e:0:function_call"``.
    :param item_type: Omnigent conversation item type, e.g.
        ``"message"`` or ``"function_call"``.
    :param data: Item payload shaped like ``SessionEventInput.data``.
    :param response_id: Synthetic response id used to group the
        Claude turn in AP/web UI rendering.
    """

    source_id: str
    item_type: str
    data: dict[str, Any]
    response_id: str

@dataclass(frozen=True)
class TranscriptReadResult:
    """
    Result of reading Claude transcript JSONL records.

    :param line_cursor: Count of complete newline-terminated records
        consumed from the transcript, e.g. ``12``.
    :param byte_offset: Byte offset immediately after the last
        complete record consumed, e.g. ``4096``. A partial trailing
        line is not included.
    :param current_response_id: Response id for a Claude assistant
        turn that remains active across polls.
    :param items: Parsed Omnigent conversation items from the
        complete records after the caller's cursor.
    :param latest_usage: Token-usage from the most recent assistant
        entry with a ``message.usage`` block. Keys: ``context_tokens``,
        ``input_tokens``, ``output_tokens``. ``None`` when no such
        entry was scanned.
    :param latest_model: ``message.model`` from the most recent
        assistant entry, or ``None``.
    """

    line_cursor: int
    byte_offset: int
    current_response_id: str | None
    items: list[ClaudeTranscriptItem]
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None

@dataclass(frozen=True)
class ClaudeHookRecord:
    """
    One complete hook JSONL record read from ``hooks.jsonl``.

    :param event_cursor: Count of complete hook records consumed
        through this record, e.g. ``3``.
    :param byte_offset: Byte offset immediately after this complete
        record, e.g. ``512``.
    :param recorded_at: Unix timestamp for when the hook was recorded,
        e.g. ``1779922393.222``. ``None`` when the envelope did not
        carry a numeric timestamp.
    :param event_name: Claude hook event name, e.g. ``"Stop"``.
        ``None`` means the line was complete but malformed or did not
        contain a usable event name; the durable cursor may still
        advance past it.
    :param source: Claude ``SessionStart`` source, e.g. ``"clear"``,
        or ``None`` for hook records without a source field.
    :param claude_session_id: Claude-native session uuid from the hook
        payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``,
        or ``None`` when absent.
    :param transcript_path: Claude transcript path from the hook
        payload, e.g. ``"/home/user/.claude/projects/x/session.jsonl"``,
        or ``None`` when absent.
    :param previous_claude_session_id: Claude session id that was
        active immediately before this hook, e.g.
        ``"a1b2c3d4-1234-5678-9abc-def012345678"``, or ``None``
        when the hook did not capture one.
    :param claude_session_was_seen: Whether the incoming Claude
        session id had already been observed before this hook was
        recorded. ``None`` means the hook did not capture that
        context.
    :param clear_rotated_to: Omnigent session id created synchronously by the
        hook for ``SessionStart source=clear``, e.g. ``"conv_new"``,
        or ``None`` when the background forwarder should rotate.
    :param fork_detected: Whether the hook identified this record as a
        Claude branch/fork transition before recording it. The
        background forwarder uses this annotation because state.json
        already points at the new Claude session by the time it reads
        hooks.jsonl.
    :param fork_rotated_to: Omnigent session id created synchronously by the
        hook for a Claude branch/fork transition, e.g. ``"conv_fork"``,
        or ``None`` when the background forwarder should fork.
    :param todos: Updated todo list from a ``PostToolUse``/``TodoWrite``
        hook event, e.g.
        ``[{"content": "Write tests", "status": "in_progress",
        "activeForm": "Writing tests"}]``. ``None`` for all other events.
    :param task_id: Native task id from a ``TaskCreated``,
        ``TaskCompleted``, or ``PostToolUse``/``TaskUpdate`` hook event,
        e.g. ``"1"``. ``None`` for all other events.
    :param task_subject: Human-readable task subject from a
        ``TaskCreated`` hook event, e.g. ``"Create folder 'abc'"``.
        ``None`` for all other events.
    :param task_status: Task status from a ``TaskCreated`` (``"pending"``),
        ``TaskCompleted`` (``"completed"``), or
        ``PostToolUse``/``TaskUpdate`` event (``"in_progress"`` or
        ``"completed"``). ``None`` for all other events.
    """

    event_cursor: int
    byte_offset: int
    event_name: str | None
    recorded_at: float | None = None
    source: str | None = None
    claude_session_id: str | None = None
    transcript_path: Path | None = None
    previous_claude_session_id: str | None = None
    claude_session_was_seen: bool | None = None
    clear_rotated_to: str | None = None
    fork_detected: bool = False
    fork_rotated_to: str | None = None
    todos: list[dict[str, Any]] | None = None
    task_id: str | None = None
    task_subject: str | None = None
    task_status: str | None = None

@dataclass(frozen=True)
class HookReadResult:
    """
    Result of reading Claude hook JSONL records.

    :param event_cursor: Count of complete hook records consumed.
    :param byte_offset: Byte offset immediately after the last
        complete hook record consumed. A partial trailing line is not
        included.
    :param records: Complete hook records after the caller's cursor.
    """

    event_cursor: int
    byte_offset: int
    records: list[ClaudeHookRecord]

@dataclass(frozen=True)
class _JsonlRecord:
    """
    One complete newline-terminated JSONL record.

    :param line_number: One-based line number relative to the reader's
        line cursor, e.g. ``5``.
    :param byte_offset: Byte offset where the record starts.
    :param next_byte_offset: Byte offset immediately after the
        newline-terminated record.
    :param text: UTF-8 decoded JSONL text including the trailing
        newline, or ``None`` when the complete record was not valid
        UTF-8 and should advance cursors without being parsed.
    """

    line_number: int
    byte_offset: int
    next_byte_offset: int
    text: str | None

@dataclass(frozen=True)
class _JsonlReadResult:
    """
    Complete-record read result for an append-only JSONL file.

    :param line_cursor: Count of complete records consumed.
    :param byte_offset: Byte offset after the last complete record.
    :param records: Complete records read after the requested byte
        offset.
    """

    line_cursor: int
    byte_offset: int
    records: list[_JsonlRecord]

@dataclass(frozen=True)
class ClaudeMessageDelta:
    """
    One streamed assistant-text chunk recorded by the MessageDisplay hook.

    Written to ``<bridge_dir>/message_deltas.jsonl`` by
    :mod:`omnigent.claude_native_message_display_hook` and read back by
    the transcript forwarder to publish ``response.output_text.delta``
    events.

    :param message_id: Claude's stable per-assistant-message id, e.g.
        ``"2ca51d97-2f0f-493a-aed7-85a5b56c5747"``. Used by the web UI
        to scope the in-flight buffer for one message; it does NOT
        appear in the transcript JSONL, so the final item is correlated
        positionally rather than by this id.
    :param index: 0-based chunk order within the message, e.g. ``3``.
    :param final: ``True`` on the last chunk of the message.
    :param delta: Incremental text for this chunk, e.g.
        ``"Pour in the wine"``. Disjoint from other chunks' text.
    """

    message_id: str
    index: int
    final: bool
    delta: str

@dataclass(frozen=True)
class MessageDeltaReadResult:
    """
    Complete-record read result for the message-deltas JSONL file.

    :param byte_offset: Byte offset after the last complete record, to
        be persisted and passed to the next read so tailing resumes
        without re-reading.
    :param deltas: Parsed deltas appended after the requested offset,
        in file (append) order.
    """

    byte_offset: int
    deltas: list[ClaudeMessageDelta]

class ClaudeNativeToolRelay:
    """
    HTTP relay for Claude MCP tool calls, scoped to its caller's lifetime.

    Claude's MCP helper process calls the relay synchronously when Claude
    Code invokes a relayed Omnigent tool; the relay forwards the call
    into the ``tool_executor`` callback supplied at start, which dispatches
    it on the runner event loop (e.g. through the Omnigent REST API).

    Callers choose the lifetime and call :meth:`close` when it ends. The
    comment-tool relay (``list_comments`` / ``update_comment``) is
    session-scoped — started when the Claude terminal launches and closed
    on session delete — whereas a per-turn caller would start and close it
    within one turn.

    :param bridge_dir: Bridge directory containing
        ``tool_relay.json``, e.g. ``/tmp/omnigent/claude-native/x``.
    :param httpd: Started localhost HTTP server for tool calls. Its bound
        address identifies this relay's advertisement on close.
    """

    def __init__(self, *, bridge_dir: Path, httpd: ThreadingHTTPServer) -> None:
        """
        Initialize the relay handle.

        :param bridge_dir: Bridge directory containing the relay
            advertisement, e.g. ``Path("/tmp/omnigent/...")``.
        :param httpd: Started localhost HTTP server for tool calls.
        :returns: None.
        """
        self._bridge_dir = bridge_dir
        self._httpd = httpd

    def close(self) -> None:
        """
        Stop the relay's HTTP server and remove its advertisement file.

        Only unlinks ``tool_relay.json`` when it still advertises *this*
        relay (its ``url`` matches this server's bound address). Sessions
        that fork/clear/resume keep the same ``bridge_id`` — hence the same
        bridge dir and relay file — so a newer session's relay may have
        overwritten the file with its own address. Unlinking unconditionally
        would delete the still-active session's advertisement and make its
        comment tools vanish. The HTTP server is always shut down (it is this
        relay's own socket).

        :returns: None.
        """
        relay_file = self._bridge_dir / _TOOL_RELAY_FILE
        host, port = self._httpd.server_address
        # A newer relay that overwrote the file advertises a different url
        # (this relay's socket is still bound, so its port is unique), so the
        # file is left for that relay to own.
        if _read_json_file(relay_file).get("url") == f"http://{host}:{port}":
            with contextlib.suppress(FileNotFoundError):
                relay_file.unlink()
        self._httpd.shutdown()
        self._httpd.server_close()


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
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
    for _key, _value in _sib_hooks.__dict__.items():
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

_wire_sibling_modules()
