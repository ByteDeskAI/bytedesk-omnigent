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

def url_component(value: str) -> str:
    """
    Percent-encode one URL path component.

    :param value: Raw path component, e.g. ``"conv_abc123"``.
    :returns: URL-safe component with slashes escaped.
    """
    return urllib.parse.quote(value, safe="")

def augment_claude_args(
    claude_args: tuple[str, ...],
    *,
    bridge_dir: Path,
    python_executable: str | None = None,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    api_key_helper: str | None = None,
    bundle_dir: Path | None = None,
    agent_name: str | None = None,
    skills_filter: str | list[str] = "all",
) -> list[str]:
    """
    Return Claude CLI args with Omnigent MCP/hook/skill injection.

    :param claude_args: User-provided Claude Code args, e.g.
        ``("--resume", "abc")``.
    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run helper
        modules. ``None`` uses :data:`sys.executable`.
    :param ap_server_url: Omnigent server base URL passed through to
        :func:`build_hook_settings` so the ``PermissionRequest``
        command hook is registered. ``None`` omits the hook and
        Claude falls back to its built-in TUI prompt.
    :param ap_auth_headers: Auth headers for the
        ``PermissionRequest`` command hook. Passed through to
        :func:`build_hook_settings`.
    :param api_key_helper: Optional Claude Code ``apiKeyHelper``
        command from ucode state, e.g. ``"databricks auth token
        --host https://example.databricks.com ..."``.
    :param bundle_dir: Materialized agent-bundle root, when the
        session's agent ships a ``skills/`` directory. Triggers
        ``--plugin-dir <bundle>`` so Claude Code discovers bundled
        skills natively — the CLI mirror of the SDK executor's plugin
        wiring. ``None`` (e.g. the ``omnigent claude`` CLI's minimal
        spec) adds no plugin args.
    :param agent_name: Agent display name for the bundle's plugin
        manifest, e.g. ``"researcher"``. ``None`` falls back to the
        bundle directory's basename.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), mapped to
        ``--setting-sources`` exactly as the SDK executor maps it onto
        ``setting_sources``. Defaults to ``"all"``.
    :returns: Augmented argument list for the terminal resource.
    """
    mcp_config = build_mcp_config(bridge_dir, python_executable=python_executable)
    hook_settings = build_hook_settings(
        bridge_dir,
        python_executable=python_executable,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        api_key_helper=api_key_helper,
    )
    args = _merge_disallowed_tools(list(claude_args), _OMNIGENT_DISALLOWED_TOOLS)
    args.extend(
        [
            "--mcp-config",
            json.dumps(mcp_config, separators=(",", ":")),
            "--settings",
            json.dumps(hook_settings, separators=(",", ":")),
        ]
    )
    args.extend(
        claude_native_skill_args(
            bundle_dir,
            agent_name=agent_name,
            skills_filter=skills_filter,
        )
    )
    return args

def _merge_disallowed_tools(args: list[str], extra: tuple[str, ...]) -> list[str]:
    """
    Add ``extra`` tool names to a ``--disallowedTools`` flag in ``args``.

    Merges into an existing flag if present (deduping while preserving
    order) so a user-supplied ``--disallowedTools`` is not silently
    overridden; otherwise appends a new flag.

    :param args: Claude CLI argument list to mutate-and-return.
    :param extra: Tool names Omnigent wants disabled.
    :returns: ``args`` with the merged flag.
    """
    if not extra:
        return args
    try:
        idx = args.index("--disallowedTools")
    except ValueError:
        args.extend(["--disallowedTools", ",".join(extra)])
        return args
    value_idx = idx + 1
    if value_idx >= len(args):
        return args
    existing = [t for t in args[value_idx].split(",") if t]
    args[value_idx] = ",".join(dict.fromkeys([*existing, *extra]))
    return args

def _wait_for_server_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, Any]:
    """
    Wait for the bridge control HTTP endpoint file.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait, e.g. ``30.0``.
    :returns: Parsed server-info JSON object.
    :raises RuntimeError: If the server file never appears.
    """
    deadline = time.monotonic() + timeout_s
    path = bridge_dir / _SERVER_FILE
    while time.monotonic() < deadline:
        payload = _read_json_file(path)
        if isinstance(payload, dict) and payload.get("url") and payload.get("token"):
            return payload
        time.sleep(0.05)
    raise RuntimeError(
        "Claude native bridge is not ready yet. Wait for Claude Code "
        "startup to finish before notifying tool list changes."
    )

def _read_json_file(path: Path) -> dict[str, Any]:
    """
    Read a JSON object file.

    :param path: JSON file path.
    :returns: Parsed object, or ``{}`` when missing/malformed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}

def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """
    Atomically write a JSON object file with owner-only permissions.

    :param path: JSON file path.
    :param payload: JSON-compatible object.
    :returns: None.
    """
    _ensure_secure_dir(path.parent)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(payload, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
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
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
