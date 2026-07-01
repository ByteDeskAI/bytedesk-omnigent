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

def _run_tmux(socket_path: str, *args: str) -> None:
    """
    Invoke ``tmux -S <socket_path> <args...>`` and raise on failure.

    :param socket_path: Absolute path to the tmux socket the terminal
        was launched on, e.g. ``"/tmp/.../tmux.sock"``.
    :param args: Arguments after ``tmux -S <socket_path>``, e.g.
        ``("send-keys", "-l", "-t", "claude:0.0", "hello")``.
    :returns: None.
    :raises RuntimeError: If the subprocess exits non-zero or times
        out.
    """
    import subprocess

    cmd = ["tmux", "-S", socket_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")

def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """
    Capture the current visible contents of a tmux pane.

    Unlike :func:`_run_tmux`, this returns stdout instead of raising on
    output, and never raises — a transient capture failure during boot
    should be treated as "not ready yet" by the caller, not an error.

    :param socket_path: Absolute path to the tmux socket, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :returns: The pane's visible text, or ``""`` if capture failed.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-t", tmux_target, "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""

def _claude_prompt_rendered(pane: str) -> bool:
    """
    Return whether Claude Code's input prompt is rendered in a pane.

    Scans the last :data:`_PROMPT_SCAN_TAIL_LINES` non-empty lines for
    :data:`_CLAUDE_PROMPT_GLYPH`. Restricting to the tail avoids false
    positives from the glyph appearing in scrollback (e.g. echoed in a
    prior response), since the live input box always sits at the bottom.

    :param pane: Captured pane text from :func:`_capture_pane`.
    :returns: ``True`` when the input box appears mounted.
    """
    non_empty = [line for line in pane.splitlines() if line.strip()]
    return any(_CLAUDE_PROMPT_GLYPH in line for line in non_empty[-_PROMPT_SCAN_TAIL_LINES:])

def _submit_needle(content: str) -> str:
    r"""
    Derive a short marker string used to spot a draft in the input box.

    Takes the first non-empty line of *content* (after the same
    line-ending normalization the paste payload gets), truncated at the
    first control character and to :data:`_DRAFT_NEEDLE_MAX_CHARS`, so
    it matches what Claude Code renders verbatim on the prompt row.

    :param content: Raw user text, possibly multi-line,
        e.g. ``"fix the bug\nin foo.py"``.
    :returns: The needle, e.g. ``"fix the bug"``. Empty string when no
        usable line exists (whitespace-only content) — callers must
        then skip draft-visibility checks.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        # Truncate at the first control char (e.g. an interior tab):
        # the TUI renders those differently, so they can't be matched
        # verbatim against the captured pane text.
        for idx, ch in enumerate(line):
            if ord(ch) < 0x20:
                line = line[:idx]
                break
        line = line.strip()
        if line:
            return line[:_DRAFT_NEEDLE_MAX_CHARS]
    return ""

def _draft_in_input_box(pane: str, needle: str) -> bool:
    """
    Return whether the pasted draft is visible in Claude's input box.

    Looks only at the **last** line containing
    :data:`_CLAUDE_PROMPT_GLYPH` — the live input box always sits at
    the bottom of the pane, below the transcript, so this never
    matches the submitted message's transcript echo. The draft counts
    as visible when the text after the glyph contains *needle* (small
    pastes render verbatim) or the
    :data:`_PASTED_PLACEHOLDER_PREFIX` placeholder (Claude Code
    collapses large pastes).

    :param pane: Captured pane text from :func:`_capture_pane`.
    :param needle: Marker from :func:`_submit_needle`, e.g.
        ``"fix the bug"``. Empty means the draft can't be identified;
        only the paste placeholder is then considered.
    :returns: ``True`` when the draft is still sitting in the input box.
    """
    glyph_lines = [line for line in pane.splitlines() if _CLAUDE_PROMPT_GLYPH in line]
    if not glyph_lines:
        return False
    tail = glyph_lines[-1].rsplit(_CLAUDE_PROMPT_GLYPH, 1)[1]
    if _PASTED_PLACEHOLDER_PREFIX in tail:
        return True
    return bool(needle) and needle in tail

def _wait_for_claude_prompt_ready(
    socket_path: str,
    tmux_target: str,
    *,
    timeout_s: float,
) -> None:
    """
    Block until Claude Code's TUI input box is ready for keystrokes.

    The runner advertises ``tmux.json`` as soon as the tmux session
    exists, but Claude Code's input box mounts a few seconds later
    (longer on a cold first boot). Keystrokes sent into that gap are
    dropped, so the first web-UI message silently vanishes. This gate
    polls ``capture-pane`` for the input prompt before injection;
    it returns immediately once mounted, so 2nd+ messages are
    unaffected.

    Claude-native only — this is called from :func:`inject_user_message`,
    which exclusively serves the Claude Code terminal. It must never be
    used for generic terminals, whose programs never render
    :data:`_CLAUDE_PROMPT_GLYPH` and would always time out.

    :param socket_path: Absolute path to the tmux socket, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :param timeout_s: Seconds to wait for the prompt, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the prompt never renders within
        *timeout_s* (Claude failed to boot).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _claude_prompt_rendered(_capture_pane(socket_path, tmux_target)):
            return
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    raise RuntimeError(
        f"Claude Code terminal did not become ready within {timeout_s}s "
        "(input prompt never rendered). The message was not delivered."
    )

def _paste_payload_bytes(text: str) -> bytes:
    r"""
    Encode text as the paste-buffer byte payload for ``tmux load-buffer``.

    Returns only the content bytes — ``paste-buffer -p`` adds the
    ``ESC [ 2 0 0 ~`` / ``ESC [ 2 0 1 ~`` bracketed-paste markers
    itself when delivering the buffer to the pane.

    Content bytes are mapped so Claude Code's TUI keeps the paste as
    editable data rather than submitting on each line:

    - ``\n`` and ``\r`` (and a ``\r\n`` pair coalesced) become a single
      carriage return ``0x0d`` — the byte a real paste carries between
      lines inside the markers.
    - ``\t`` becomes ``0x09``.
    - Any other control byte below ``0x20`` is dropped: a stray ``ESC``
      (or BEL, etc.) in the content would otherwise prematurely close
      the bracketed-paste sequence on the agent's side.
    - All other characters pass through as their UTF-8 bytes.

    :param text: Raw user text, possibly multi-line, e.g.
        ``"line one\nline two"`` or ``"a\r\nb"``.
    :returns: The normalized content bytes, e.g. ``b"line one\rline two"``.
    """
    # Normalize line endings to a single "\n" first so CRLF / lone CR
    # pastes don't double up: every line break becomes exactly one CR.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
            continue
        if ch == "\t":
            body.append(0x09)
            continue
        if ord(ch) < 0x20:
            continue
        body.extend(ch.encode("utf-8"))
    return bytes(body)

def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """
    Wait for the runner to write ``tmux.json``.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait, e.g. ``30.0``.
    :returns: ``{"socket_path": ..., "tmux_target": ...}``.
    :raises RuntimeError: If the file never appears with valid
        ``socket_path`` and ``tmux_target`` fields.
    """
    deadline = time.monotonic() + timeout_s
    path = bridge_dir / _TMUX_FILE
    while time.monotonic() < deadline:
        payload = _read_json_file(path)
        socket_path = payload.get("socket_path") if isinstance(payload, dict) else None
        tmux_target = payload.get("tmux_target") if isinstance(payload, dict) else None
        if isinstance(socket_path, str) and isinstance(tmux_target, str):
            return {"socket_path": socket_path, "tmux_target": tmux_target}
        time.sleep(0.05)
    raise RuntimeError(
        "Claude terminal tmux target is not advertised yet. Wait for the "
        "terminal to launch before sending messages from the web UI."
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
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_inject.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
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
