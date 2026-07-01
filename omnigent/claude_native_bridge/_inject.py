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

def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    r"""
    Deliver a user message into the Claude terminal via tmux send-keys.

    Before typing, this waits for two readiness conditions: the runner
    advertising ``tmux.json``, then Claude Code's input box rendering
    (see :func:`_wait_for_claude_prompt_ready`). The second gate closes
    a race on freshly-created sessions where the first message would
    otherwise be typed into a still-booting TUI and silently dropped.

    Delivered as one bracketed paste via ``tmux load-buffer`` (from a
    temp file) + ``paste-buffer -p`` so interior newlines ride as raw CR
    inside the paste markers and Claude Code's TUI keeps multi-line
    input as data rather than submitting on each newline
    (anthropics/claude-code#52126). A trailing newline inside the paste
    absorbs any trailing backslash — otherwise ``\`` + the submit
    ``Enter`` reads as a line-continuation and the message sits unsent.
    ``Enter`` is a separate tmux call. The file-based buffer
    path (not ``send-keys`` argv) matters: tmux caps a single
    client→server command at ~16KB, so a large message — e.g. a PR diff
    in a sub-agent dispatch — failed with "command too long".

    The submit is **verified, not fire-and-forget**: Claude Code
    coalesces rapid stdin bursts into a paste, so an Enter that lands
    while the TUI is still consuming the paste is folded in as a
    newline and the draft sits unsent. This helper first polls
    ``capture-pane`` until the draft is visible in the input box (the
    paste was committed), sends Enter, then polls that the draft left
    the box — re-sending Enter while it hasn't — and raises if the
    message never submits.

    :param bridge_dir: Bridge directory path.
    :param content: User text from the Omnigent web UI. Must be non-empty.
    :param timeout_s: Seconds to wait for each readiness gate
        (``tmux.json`` advertised, then prompt rendered), e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in time,
        if Claude's input prompt never renders, if a ``tmux send-keys``
        invocation fails, or if the draft never leaves the input box
        after repeated submit Enters (message not delivered).
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # tmux.json only means the tmux session exists; Claude Code's input
    # box mounts a few seconds later. Block until the prompt renders so
    # the first message isn't typed into a still-booting TUI and dropped.
    _wait_for_claude_prompt_ready(
        info["socket_path"],
        info["tmux_target"],
        timeout_s=timeout_s,
    )
    # Clear any leftover text in Claude's input field before typing.
    # After Escape-cancel, Claude Code re-populates the prompt area
    # with the previous input for re-editing. Without this clear,
    # the new message appends to the stale buffer (e.g.
    # "old promptnew prompt" with no separator).
    # Ctrl-A (Home) + Ctrl-K (kill-to-end) is the safest pair —
    # Ctrl-U only clears backwards from cursor.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-a")
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-k")
    # Trailing newline absorbs a trailing "\" so it can't escape the submit Enter.
    # Delivered through a tmux buffer, NOT ``send-keys`` argv: tmux caps one
    # client→server command at ~16KB, so per-byte hex argv blew up with
    # "command too long" on large payloads (a PR diff in a sub-agent
    # dispatch). ``load-buffer`` streams the file without that cap, and
    # ``paste-buffer -p`` wraps it in the same bracketed-paste markers so
    # interior newlines (mapped to CR below) stay data instead of becoming
    # per-line submits. See anthropics/claude-code#52126.
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(info["socket_path"], "load-buffer", "-b", "omnigent-paste", paste_path)
        _run_tmux(
            info["socket_path"],
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting (no stale copies server-side)
            "-b",
            "omnigent-paste",
            "-t",
            info["tmux_target"],
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    # Wait until the TUI has visibly committed the paste into its input
    # box before submitting. Claude Code coalesces rapid stdin bursts
    # into a paste; an Enter that arrives while it is still consuming
    # the paste becomes a newline inside the draft instead of a submit,
    # and the message sits unsent. A fixed sleep raced this (lost under
    # load / large payloads); polling is deterministic. Best-effort:
    # when the draft never becomes identifiable (e.g. whitespace-only
    # first line, custom statusline containing the glyph), fall through
    # after the timeout and submit blind, matching the old behavior.
    needle = _submit_needle(content)
    draft_seen = False
    deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if _draft_in_input_box(_capture_pane(info["socket_path"], info["tmux_target"]), needle):
            draft_seen = True
            break
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
    if not draft_seen:
        # The draft was never observed, so its absence proves nothing —
        # verification would trivially "pass". Submit blind as before.
        return
    # Verify the submit took: a successful Enter clears the input box.
    # If the draft is still sitting there the Enter was swallowed into
    # the paste burst as a newline — re-send it (the retry lands well
    # after the burst, so it submits). Each Enter only fires while the
    # draft is verifiably still present, so a retry can never hit an
    # empty prompt or a permission dialog of the started turn.
    deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
    last_enter = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
        pane = _capture_pane(info["socket_path"], info["tmux_target"])
        if not _draft_in_input_box(pane, needle):
            return
        if time.monotonic() - last_enter >= _SUBMIT_RETRY_INTERVAL_S:
            _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
            last_enter = time.monotonic()
    raise RuntimeError(
        f"Claude Code did not accept the submitted message within {_SUBMIT_VERIFY_TIMEOUT_S}s "
        "(the draft is still in the input box). The message was not delivered."
    )

def inject_interrupt(
    bridge_dir: Path,
    *,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Send an Escape keystroke into the Claude terminal via tmux send-keys.

    Claude Code's TUI cancels an in-flight response on a single
    ``Escape``. The harness's ``run_turn`` for ``claude-native``
    returns immediately after the tmux paste (the long-running work
    happens inside the ``claude`` binary in the pane, not the
    harness), so the scaffold's interrupt path can't reach it — this
    helper is the analog of :func:`inject_user_message` for the AP
    web stop button / Escape keybind.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param timeout_s: Seconds to wait for ``tmux.json`` to be
        advertised by the runner, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if the ``tmux send-keys`` invocation fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")

def kill_session(
    bridge_dir: Path,
    *,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Forcefully terminate the Claude tmux session via ``kill-session``.

    Claude-native sessions run the ``claude`` binary inside a tmux
    session on a per-session socket (see
    :class:`omnigent.inner.terminal.TerminalInstance`). The only way
    a user can end such a session today is to re-attach to the tmux in
    their terminal and exit from inside it. This helper is the analog
    of that manual exit for the Omnigent web UI's "Stop session" affordance:
    it kills the tmux session outright, which terminates ``claude`` and
    everything in the pane.

    Unlike :func:`inject_interrupt` (which sends a single ``Escape`` to
    cancel an in-flight response but leaves the session alive), this is
    a hard stop. Once the pane is gone the wrapper's reconnect loop
    observes the terminal resource disappear and tears the session
    down through its normal end-of-session path, so no transcript items
    are synthesized here.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param timeout_s: Seconds to wait for ``tmux.json`` to be
        advertised by the runner, e.g. ``30.0``. A short value is
        appropriate for the UI path — a missing ``tmux.json`` means
        there is no live session to kill.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if the ``tmux kill-session`` invocation fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])

def inject_slash_command(
    bridge_dir: Path,
    *,
    command: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
    auto_confirm: bool = False,
) -> None:
    """
    Type a Claude Code slash command into the tmux pane and submit it.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param command: Single-line slash command including the leading
        ``/``, e.g. ``"/effort high"``.
    :param timeout_s: Seconds to wait for ``tmux.json``, e.g. ``30.0``.
    :param auto_confirm: If ``True``, send an extra ``Enter`` after a
        short delay to accept the default option of any TUI confirmation
        dialog that the command may pop (e.g. ``/effort`` / ``/model``
        prompt when switching invalidates the prompt cache). HACK —
        the chat UI has no way to render the CLI's TUI dialog, so
        without this the command silently stalls. Assumes the default
        option is "accept" (true today for effort + model). When no
        dialog appears, the extra Enter falls on an empty prompt and is
        a no-op. Callers that don't trigger confirmations should leave
        this ``False``.
    :raises ValueError: If *command* is empty, does not start with
        ``/``, or contains a newline.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if a ``tmux send-keys`` invocation fails.
    """
    if not command or not command.startswith("/"):
        raise ValueError(f"slash command must start with '/'; got {command!r}")
    if "\n" in command:
        raise ValueError("slash command must be a single line")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # ``C-u`` clears any draft the user is mid-typing; otherwise the
    # paste below concatenates with their text and Enter submits
    # ``<their-draft>/effort high`` as a turn. Unlike Escape it does
    # not interrupt an in-flight generation.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-u")
    # ``-l`` pastes ``/`` and spaces literally; trailing Enter submits.
    _run_tmux(info["socket_path"], "send-keys", "-l", "-t", info["tmux_target"], command)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
    if auto_confirm:
        # Give the TUI time to render its confirmation dialog before
        # the auto-Enter arrives; otherwise the keystroke races the
        # prompt and gets dropped.
        time.sleep(0.3)
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")

def display_cost_approval_popup(
    bridge_dir: Path,
    *,
    session_id: str,
    elicitation_id: str,
    message: str,
    policy_name: str | None = None,
    python_executable: str | None = None,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Overlay a cost-budget approval modal on the Claude Code tmux pane.

    Launches :mod:`omnigent.native_cost_popup` inside a
    ``tmux display-popup``, so a user working in the native terminal —
    not only the web ``ApprovalCard`` — can approve/decline a cost
    checkpoint. The popup script resolves the **same** elicitation Future
    (via the same resolve endpoint the web card uses), so whichever
    surface answers first wins and the other clears. The popup reads AP
    routing (base URL + auth headers) from this bridge's
    ``permission_hook.json`` so no token lands on the command line.

    Fire-and-forget by design: ``tmux display-popup`` blocks its tmux
    client until the popup closes, so it is spawned **detached**
    (``Popen``, not awaited) — the caller returns immediately while the
    modal lives on the attached client until the user answers.

    Claude-native resolver for the harness-agnostic
    :func:`omnigent.native_cost_popup.launch_cost_popup`: it reads the
    pane's tmux socket/target from this bridge's ``tmux.json`` and points
    the popup at this bridge's ``permission_hook.json`` for Omnigent routing
    (base URL + auth headers, so no token lands on the command line), then
    delegates. The launcher pops the modal on every attached client and
    skips silently when none is attached (e.g. the Terminal tab is closed)
    — the web ``ApprovalCard`` remains the answer surface.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``. Supplies both the
        tmux target (``tmux.json``) and the AP-routing config
        (``permission_hook.json``).
    :param session_id: Omnigent session id that owns the elicitation, e.g.
        ``"conv_abc123"``. Used in the resolve URL the popup POSTs to.
    :param elicitation_id: Outstanding elicitation correlation id, e.g.
        ``"elicit_deadbeef"``.
    :param message: Approval reason shown in the popup, e.g.
        ``"Session cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
    :param policy_name: Name of the deciding policy, rendered as the
        modal header. ``None`` falls back to a generic header.
    :param python_executable: Python used to run the popup module;
        ``None`` uses :data:`sys.executable` (the runner's interpreter,
        valid on the host the tmux server runs on).
    :param timeout_s: Seconds to wait for ``tmux.json`` to be advertised,
        e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised within
        *timeout_s* (the pane isn't up yet); the caller treats this as a
        best-effort miss and the web card remains answerable.
    """
    from omnigent.native_cost_popup import launch_cost_popup

    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    launch_cost_popup(
        info["socket_path"],
        info["tmux_target"],
        bridge_dir / _PERMISSION_HOOK_FILE,
        session_id=session_id,
        elicitation_id=elicitation_id,
        message=message,
        policy_name=policy_name,
        python_executable=python_executable,
    )

def post_tools_changed(
    bridge_dir: Path,
    *,
    timeout_s: float = _TOOLS_CHANGED_READY_TIMEOUT_S,
) -> None:
    """
    Notify Claude Code that the MCP tool list changed.

    Standard MCP ``notifications/tools/list_changed`` — the bridge's
    localhost HTTP control endpoint trampolines the POST into the
    MCP stdio writer. Unrelated to Claude's experimental Channels.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait for the bridge HTTP control
        endpoint to publish itself, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the bridge server is not ready or
        rejects the notification.
    """
    server = _wait_for_server_info(bridge_dir, timeout_s=timeout_s)
    token = server.get("token")
    url = server.get("url")
    if not isinstance(token, str) or not isinstance(url, str):
        raise RuntimeError("Claude native bridge server file is missing url/token")
    req = request.Request(
        f"{url}/tools-changed",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=_TOOLS_CHANGED_POST_TIMEOUT_S) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"tools-changed POST failed with HTTP {resp.status}")
    except error.URLError as exc:
        raise RuntimeError(f"failed to notify Claude tool list change: {exc}") from exc


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
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
    for _key, _value in _sib_hooks.__dict__.items():
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
