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

def count_transcript_lines(transcript_path: Path) -> int:
    """
    Count JSONL records currently present in a Claude transcript.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :returns: Number of newline-delimited records. Missing files
        count as zero.
    """
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except FileNotFoundError:
        return 0

def transcript_has_recent_local_command(
    transcript_path: Path,
    *,
    claude_session_id: str,
    recorded_at: float,
    command_names: frozenset[str],
) -> bool:
    """
    Return whether Claude recently recorded one local command.

    :param transcript_path: Claude transcript JSONL path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param claude_session_id: Claude-native session uuid from the
        hook payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :param recorded_at: Unix timestamp for the hook record,
        e.g. ``1779922393.222``.
    :param command_names: Slash-command names to match, including
        the leading slash, e.g. ``frozenset({"/fork", "/branch"})``.
    :returns: ``True`` when a matching ``local_command`` transcript
        record exists near ``recorded_at`` for ``claude_session_id``.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines[-_RECENT_LOCAL_COMMAND_LINE_LIMIT:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("sessionId") != claude_session_id:
            continue
        if record.get("subtype") != "local_command":
            continue
        timestamp = _transcript_timestamp(record.get("timestamp"))
        if timestamp is None or abs(timestamp - recorded_at) > _RECENT_LOCAL_COMMAND_WINDOW_S:
            continue
        content = record.get("content")
        if not isinstance(content, str):
            continue
        command_name = _local_command_name(content)
        if command_name in command_names:
            return True
    return False

def transcript_has_forked_from_marker(
    transcript_path: Path,
    *,
    claude_session_id: str,
    source_claude_session_id: str | None,
) -> bool:
    """
    Return whether Claude marked a transcript as a fork.

    Claude branch/fork transcripts carry structured ``forkedFrom``
    metadata on copied records. This is the stable non-title signal
    that a ``SessionStart source=resume`` event represents a new
    branch rather than an ordinary resume.

    :param transcript_path: Claude transcript JSONL path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param claude_session_id: New Claude-native session uuid from the
        hook payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :param source_claude_session_id: Expected source Claude session
        uuid, e.g. ``"9abc..."``. ``None`` accepts any different
        non-empty source id.
    :returns: ``True`` when the transcript records a fork from the
        expected source session.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in _sample_transcript_edges(lines, _FORKED_FROM_LINE_LIMIT):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("sessionId") != claude_session_id:
            continue
        forked_from = record.get("forkedFrom")
        if not isinstance(forked_from, dict):
            continue
        raw_source_session_id = forked_from.get("sessionId")
        if not isinstance(raw_source_session_id, str) or not raw_source_session_id:
            continue
        if raw_source_session_id == claude_session_id:
            continue
        if (
            source_claude_session_id is not None
            and raw_source_session_id != source_claude_session_id
        ):
            continue
        return True
    return False

def _sample_transcript_edges(lines: list[str], limit: int) -> list[str]:
    """
    Return transcript lines from the start and end of a file.

    :param lines: Transcript JSONL lines.
    :param limit: Maximum number of lines to take from each edge,
        e.g. ``200``.
    :returns: Sampled lines, preserving file order.
    """
    if limit <= 0 or len(lines) <= limit * 2:
        return lines
    return [*lines[:limit], *lines[-limit:]]

def _transcript_timestamp(value: object) -> float | None:
    """
    Parse a Claude transcript timestamp.

    :param value: Timestamp string, e.g.
        ``"2026-05-27T22:53:13.245Z"``.
    :returns: Unix timestamp, e.g. ``1779922393.245``, or ``None``
        when parsing fails.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

def _local_command_name(content: str) -> str | None:
    """
    Extract a Claude local command name from transcript content.

    :param content: Local-command transcript content, e.g.
        ``"<command-name>/fork</command-name>"``.
    :returns: Command name including leading slash, e.g.
        ``"/fork"``, or ``None`` when no command tag exists.
    """
    name_match = _COMMAND_NAME_RE.search(content)
    if name_match is None:
        return None
    name = name_match.group(1).strip()
    return name or None

def read_assistant_text_since(
    transcript_path: Path,
    start_line: int,
) -> tuple[int, list[str]]:
    """
    Read assistant text blocks appended after a transcript cursor.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: Zero-based line cursor captured before a
        message is injected into the Claude terminal.
    :returns: ``(new_cursor, text_chunks)``.
    """
    texts: list[str] = []
    cursor = 0
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            for cursor, line in enumerate(handle, start=1):
                if cursor <= start_line:
                    continue
                text = _assistant_text_from_transcript_line(line)
                if text:
                    texts.append(text)
    except FileNotFoundError:
        return start_line, []
    return cursor, texts

def read_transcript_items_since(
    transcript_path: Path,
    start_line: int,
    *,
    agent_name: str,
    current_response_id: str | None = None,
) -> tuple[int, str | None, list[ClaudeTranscriptItem]]:
    """
    Read Claude transcript records as Omnigent conversation items.

    Claude Code writes append-only JSONL records whose ``message``
    payloads include user prompts, assistant text, native tool calls,
    and native tool results. This parser intentionally ignores
    metadata records (title, file-history, permission mode, system
    bookkeeping) and raw ``thinking`` blocks, while translating the
    user-visible semantic records into Omnigent item types the web UI
    already understands.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: One-based line cursor. Lines at or before
        this cursor are skipped.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :returns: ``(new_cursor, current_response_id, items)``.
    """
    result = read_transcript_items_since_with_position(
        transcript_path,
        start_line,
        agent_name=agent_name,
        current_response_id=current_response_id,
    )
    return result.line_cursor, result.current_response_id, result.items

def read_transcript_items_since_with_position(
    transcript_path: Path,
    start_line: int,
    *,
    agent_name: str,
    current_response_id: str | None = None,
) -> TranscriptReadResult:
    """
    Read transcript items from a line cursor and return byte position.

    This compatibility reader supports existing durable state that
    only stored a line cursor. It scans the file once, parses only
    complete newline-terminated records after ``start_line``, and
    returns the byte offset so future polls can seek directly.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: One-based line cursor. Lines at or before
        this cursor are skipped.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :returns: Parsed items plus line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=0,
        start_line=0,
        emit_after_line=start_line,
    )
    items: list[ClaudeTranscriptItem] = []
    active_response_id = current_response_id
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        active_response_id, parsed = _transcript_items_from_entry(
            entry,
            line_number=record.line_number,
            record_offset=None,
            agent_name=agent_name,
            current_response_id=active_response_id,
        )
        items.extend(parsed)
        usage = _usage_from_transcript_entry(entry)
        if usage is not None:
            latest_usage = usage
        model = _model_from_transcript_entry(entry)
        if model is not None:
            latest_model = model
    return TranscriptReadResult(
        line_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        current_response_id=active_response_id,
        items=items,
        latest_usage=latest_usage,
        latest_model=latest_model,
    )

def read_transcript_items_from_offset(
    transcript_path: Path,
    byte_offset: int,
    *,
    start_line: int,
    agent_name: str,
    current_response_id: str | None = None,
    include_sidechains: bool = False,
) -> TranscriptReadResult:
    """
    Read transcript items appended after a byte offset.

    Only complete newline-terminated JSONL records are parsed. If
    Claude is midway through writing a trailing JSON record, the
    returned byte offset remains before that partial line so the next
    poll retries it after completion.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param byte_offset: Byte offset already consumed, e.g. ``4096``.
    :param start_line: Count of complete records already consumed.
        Used only to keep legacy line cursors and diagnostics
        monotonic while byte offsets drive the actual seek.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :param include_sidechains: Pass ``True`` when reading a
        sub-agent's own ``agent-<id>.jsonl`` — every record there is
        a sidechain by Claude's definition, and dropping them would
        leave the sub-agent's child Omnigent conversation empty. The
        default ``False`` keeps the parent-transcript path
        unchanged.
    :returns: Parsed items plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=byte_offset,
        start_line=start_line,
    )
    items: list[ClaudeTranscriptItem] = []
    active_response_id = current_response_id
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        active_response_id, parsed = _transcript_items_from_entry(
            entry,
            line_number=record.line_number,
            record_offset=record.byte_offset,
            agent_name=agent_name,
            current_response_id=active_response_id,
            include_sidechains=include_sidechains,
        )
        items.extend(parsed)
        usage = _usage_from_transcript_entry(entry)
        if usage is not None:
            latest_usage = usage
        model = _model_from_transcript_entry(entry)
        if model is not None:
            latest_model = model
    return TranscriptReadResult(
        line_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        current_response_id=active_response_id,
        items=items,
        latest_usage=latest_usage,
        latest_model=latest_model,
    )


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
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
