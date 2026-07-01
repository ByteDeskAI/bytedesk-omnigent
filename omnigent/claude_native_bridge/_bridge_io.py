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

def _runtime_bridge_root() -> Path:
    """Read the live Claude bridge root (monkeypatch-friendly via the package facade)."""
    import omnigent.claude_native_bridge as bridge

    return bridge._BRIDGE_ROOT


def _runtime_trusted_parent() -> Path:
    """Read the live trusted parent for Claude bridge dirs."""
    import omnigent.claude_native_bridge as bridge

    return bridge._TRUSTED_PARENT


def _absolute_syntactic_path(path: Path) -> Path:
    """
    Return an absolute path without following symlinks.

    Security validation needs to inspect symlinked ancestors with
    ``lstat``. ``Path.resolve`` would follow an existing symlink before
    that inspection, so this helper only expands ``~`` and normalizes
    ``.`` / ``..`` components.

    :param path: Path to normalize, e.g. ``Path("~/.omnigent/x")``.
    :returns: Absolute path with syntactic normalization applied.
    """
    return Path(os.path.abspath(os.fspath(path.expanduser())))

def _trusted_parent_for_bridge_dir(target: Path) -> Path:
    """
    Return the trusted parent for an allowed bridge directory.

    Claude-native files live below the uid-scoped temp bridge root.
    Codex-native reuses the relay/MCP implementation but keeps bridge
    files below ``~/.omnigent/codex-native``. Both roots use the same
    owner-only ancestor validation; only the trusted anchor differs.

    :param target: Normalized bridge directory path being created or validated,
        e.g. ``Path("/tmp/omnigent-501/claude-native/abc")``.
    :returns: Absolute parent at which ancestor validation stops, e.g.
        ``Path("/tmp")``.
    :raises RuntimeError: If ``target`` is not below a known bridge root.
    """
    claude_root = _absolute_syntactic_path(_runtime_bridge_root())
    if target.is_relative_to(claude_root):
        return _absolute_syntactic_path(_runtime_trusted_parent())

    from omnigent.codex_native_bridge import bridge_root

    codex_root = _absolute_syntactic_path(bridge_root())
    if target.is_relative_to(codex_root):
        # In production, trust $HOME and validate/chmod the two bridge-owned
        # directories below it: .omnigent and codex-native. In tests, the
        # monkeypatched root may not use that shape, so trust the direct parent.
        trusted_parent = codex_root.parent
        if codex_root.name == "codex-native" and codex_root.parent.name == ".omnigent":
            trusted_parent = codex_root.parent.parent
        return _absolute_syntactic_path(trusted_parent)

    raise RuntimeError(
        f"bridge dir {target!s} is not under an allowed bridge root "
        f"({claude_root!s}, {codex_root!s})"
    )

def _ensure_secure_dir(target: Path) -> None:
    """
    Create or validate ``target`` as an owner-only directory chain.

    ``Path.mkdir(mode=0o700, parents=True, exist_ok=True)`` only applies
    the mode to the leaf and silently trusts any pre-existing ancestor.
    On a shared host, an attacker could pre-create
    ``/tmp/omnigent-<UID>`` (Claude-native), ``~/.omnigent``
    (Codex-native), or a deeper ancestor as a symlink — or as a 0o777
    directory — and redirect the bridge tree (which stores bearer
    tokens in JSON files).

    This helper resolves the trusted parent for ``target`` and walks
    each ancestor from that trusted parent down to ``target``,
    creating new ones with mode 0o700 and rejecting any existing
    ancestor that is a symlink, not a directory, owned by a different
    uid, or has group/other permission bits set. Wrong-but-repairable
    modes on dirs we own are reset to 0o700.

    :param target: Final bridge directory path to ensure, e.g.
        ``Path("/tmp/omnigent-501/claude-native/abc")``.
    :raises RuntimeError: If validation fails for any ancestor.
    """
    target = _absolute_syntactic_path(target)
    trusted_parent = _trusted_parent_for_bridge_dir(target)
    ancestors: list[Path] = []
    cur = target
    while cur != trusted_parent and cur != cur.parent:
        ancestors.append(cur)
        cur = cur.parent
    if cur != trusted_parent:
        raise RuntimeError(f"bridge dir {target!s} is not under trusted parent {trusted_parent!s}")
    ancestors.reverse()
    my_uid = os.getuid()
    for ancestor in ancestors:
        try:
            os.mkdir(ancestor, mode=0o700)
            continue
        except FileExistsError:
            pass
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: not a directory")
        if st.st_uid != my_uid:
            raise RuntimeError(
                f"refusing to use bridge ancestor {ancestor!s}: owned by uid "
                f"{st.st_uid}, not current user ({my_uid})"
            )
        if (st.st_mode & 0o077) != 0:
            os.chmod(ancestor, 0o700)

def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the deterministic bridge directory for a Claude-native bridge.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Absolute bridge directory under
        ``/tmp/omnigent-<UID>/claude-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _runtime_bridge_root() / digest

def bridge_dir_for_conversation_id(conversation_id: str) -> Path:
    """
    Return the bridge directory for a legacy session id.

    :param conversation_id: Omnigent conversation id used as bridge id, e.g.
        ``"conv_abc123"``.
    :returns: Absolute bridge directory under
        ``/tmp/omnigent-<UID>/claude-native``.
    """
    return bridge_dir_for_bridge_id(conversation_id)

def build_claude_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``claude-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id from
        :data:`BRIDGE_ID_LABEL_KEY`, e.g. ``"bridge_abc123"``. ``None``
        normalizes old sessions by using *conversation_id*.
    :returns: Environment variables needed by
        :class:`ClaudeNativeExecutor`.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }

def prepare_bridge_dir(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
    workspace: Path,
    launch_model: str | None = None,
) -> Path:
    """
    Create or refresh the bridge directory for a native Claude session.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
        ``None`` normalizes old sessions by using *conversation_id*.
    :param workspace: Runner workspace/cwd used for local OS tools.
    :param launch_model: Gateway model name that Claude was launched
        with, e.g. ``"databricks-claude-opus-4-7"``.  Persisted so the
        forwarder can re-inject it when Claude Code's ``/model``
        normalizes the name to one the gateway rejects.  ``None`` when
        no ucode profile is active.
    :returns: Bridge directory path.
    """
    resolved_bridge_id = bridge_id or conversation_id
    bridge_dir = bridge_dir_for_bridge_id(resolved_bridge_id)
    _ensure_secure_dir(bridge_dir)
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    token = config.get("token") if isinstance(config, dict) else None
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
    payload: dict[str, object] = {
        "bridge_id": resolved_bridge_id,
        "active_session_id": conversation_id,
        "conversation_id": conversation_id,
        "workspace": str(workspace),
        "token": token,
        "updated_at": time.time(),
    }
    if launch_model is not None:
        payload["launch_model"] = launch_model
    _write_json_file(bridge_dir / _CONFIG_FILE, payload)
    # Keep ``_PERMISSION_HOOK_FILE`` — the PermissionRequest command hook
    # reads the Omnigent server URL from it at runtime, so wiping it on re-prep
    # breaks approval routing on reattach/rebind. ``build_hook_settings``
    # rewrites it on cold launch.
    for filename in (
        _SERVER_FILE,
        _STATE_FILE,
        _HOOKS_FILE,
        _TOOL_RELAY_FILE,
        _TMUX_FILE,
    ):
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / filename).unlink()
    return bridge_dir

def ensure_claude_workspace_trusted(workspace: Path) -> None:
    """
    Pre-accept Claude Code's first-run trust + onboarding prompts.

    Claude Code blocks on two TUI prompts the first time it launches in
    a new context: a global onboarding flow (theme / login) gated by the
    top-level ``hasCompletedOnboarding`` key in ``~/.claude.json``, and a
    per-directory "Do you trust the files in this folder?" dialog gated
    by ``projects["<abs cwd>"].hasTrustDialogAccepted``. Neither fires a
    ``PermissionRequest`` hook, so on a host-spawned (web-UI-driven)
    session there is nobody at the terminal to answer them: Claude hangs
    and the web UI shows nothing. This is acute with
    per-session git worktrees, which hand Claude a brand-new —
    therefore untrusted — directory on every session.

    Seed both gating keys idempotently so the launch never blocks. Only
    those two keys are written; all other ``~/.claude.json`` state (the
    user's own onboarding choices, project history, MCP config, OAuth
    account) is preserved, and the file is left untouched when both keys
    are already set. This deliberately does NOT skip per-tool permission
    prompts — those still route to the web UI via the ``PermissionRequest``
    hook; only the unhookable startup gates are pre-accepted.

    Concurrency: this is a read-modify-write of a file Claude itself also
    rewrites. It runs once, before the terminal is launched (so Claude is
    not yet writing for this session), and uses an atomic replace. Two
    runners starting on the same host within the same instant could still
    race on last-writer-wins; the only consequence is that one session may
    re-show the trust prompt, which a relaunch clears. Matching this to
    Claude's own lock-free writes keeps the helper simple.

    :param workspace: The runner workspace Claude will launch in, e.g.
        ``Path("/home/user/repo-worktrees/feature-x")``. Resolved to an
        absolute path to match Claude's ``projects`` key convention.
    :returns: None.
    :raises ValueError: If an existing ``~/.claude.json`` (or its
        ``projects`` map / target project entry) is not a JSON object.
        Surfaced rather than silently overwritten so a corrupt or
        unexpected user config is never clobbered (fail loud).
    :raises json.JSONDecodeError: If an existing ``~/.claude.json`` is
        not valid JSON, for the same reason.
    """
    config_path = Path.home() / ".claude.json"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{config_path} is not a JSON object; refusing to overwrite.")
    else:
        data = {}

    changed = False
    # Global onboarding gate (theme / login). Absent on a machine that
    # has never run Claude Code interactively.
    if data.get("hasCompletedOnboarding") is not True:
        data["hasCompletedOnboarding"] = True
        changed = True

    # Per-directory trust gate. Claude keys its ``projects`` map by the
    # resolved absolute path, so match that exactly.
    project_key = str(workspace.resolve())
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError(f"{config_path} 'projects' is not a JSON object; refusing to overwrite.")
    project = projects.setdefault(project_key, {})
    if not isinstance(project, dict):
        raise ValueError(
            f"{config_path} projects[{project_key!r}] is not a JSON object; refusing to overwrite."
        )
    if project.get("hasTrustDialogAccepted") is not True:
        project["hasTrustDialogAccepted"] = True
        changed = True

    if not changed:
        return
    _atomic_write_user_json(config_path, data)

def _atomic_write_user_json(path: Path, payload: dict[str, Any]) -> None:
    """
    Atomically rewrite a user-owned JSON config file in place.

    Unlike :func:`_write_json_file` (which targets the owner-only bridge
    tree under ``/tmp`` and enforces secure-directory ownership on the
    parent), this writes the user's own ``~/.claude.json`` in their home
    directory: it must not re-permission the home directory, but it does
    pin the result to owner-only ``0o600`` because the file holds the
    Claude OAuth account block.

    :param path: Destination file, e.g. ``Path("~/.claude.json")``.
    :param payload: JSON-serializable config object to write. Rendered
        with two-space indentation to match Claude's own formatting and
        keep diffs readable.
    :returns: None.
    :raises OSError: If the temp file cannot be written, ``chmod``-ed,
        or atomically replaced into place — e.g. the home directory is
        read-only or the filesystem does not support ``os.replace``.
    """
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
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()

def read_active_session_id(bridge_dir: Path) -> str | None:
    """
    Read the Omnigent session currently receiving bridge-originated events.

    :param bridge_dir: Bridge directory path.
    :returns: Active Omnigent session id, e.g. ``"conv_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    active = config.get("active_session_id")
    if isinstance(active, str) and active:
        return active
    legacy = config.get("conversation_id")
    return legacy if isinstance(legacy, str) and legacy else None

def read_launch_model(bridge_dir: Path) -> str | None:
    """
    Read the gateway model name that Claude was launched with.

    :param bridge_dir: Bridge directory path.
    :returns: Gateway model name, e.g.
        ``"databricks-claude-opus-4-7"``, or ``None`` when no ucode
        profile was active at launch time.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    model = config.get("launch_model")
    return model if isinstance(model, str) and model else None

def read_bridge_id(bridge_dir: Path) -> str | None:
    """
    Read the opaque bridge id from bridge config.

    :param bridge_dir: Bridge directory path.
    :returns: Opaque bridge id, e.g. ``"bridge_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    bridge_id = config.get("bridge_id")
    return bridge_id if isinstance(bridge_id, str) and bridge_id else None

def write_active_session_id(bridge_dir: Path, session_id: str) -> None:
    """
    Atomically update the bridge's active Omnigent session.

    :param bridge_dir: Bridge directory path.
    :param session_id: New active Omnigent session id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    :raises RuntimeError: If the bridge config does not exist.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not config:
        raise RuntimeError(f"bridge config missing: {bridge_dir / _CONFIG_FILE}")
    config["active_session_id"] = session_id
    config["conversation_id"] = session_id
    config["updated_at"] = time.time()
    _write_json_file(bridge_dir / _CONFIG_FILE, config)

def read_permission_hook_config(bridge_dir: Path) -> dict[str, Any]:
    """
    Read Omnigent routing details for the permission command hook.

    :param bridge_dir: Bridge directory path.
    :returns: Permission hook config, e.g.
        ``{"ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer token"}}``.
        Empty dict when the file is absent or malformed.
    """
    payload = _read_json_file(bridge_dir / _PERMISSION_HOOK_FILE)
    return payload if isinstance(payload, dict) else {}

def build_mcp_config(bridge_dir: Path, *, python_executable: str | None = None) -> dict[str, Any]:
    """
    Build the Claude Code MCP config for the Omnigent bridge server.

    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :returns: JSON-serializable Claude MCP config.
    """
    python = python_executable or sys.executable
    return {
        "mcpServers": {
            _MCP_SERVER_NAME: {
                "command": python,
                "args": [
                    "-I",
                    "-m",
                    "omnigent.claude_native_bridge",
                    "serve-mcp",
                    "--bridge-dir",
                    str(bridge_dir),
                ],
                "env": {
                    "PYTHONUNBUFFERED": "1",
                },
            }
        }
    }

def build_hook_settings(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    api_key_helper: str | None = None,
) -> dict[str, Any]:
    """
    Build invocation-local Claude Code hook settings.

    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :param ap_server_url: Omnigent server base URL the ``PermissionRequest``
        command hook should POST to, e.g. ``"http://127.0.0.1:8787"``.
        When ``None``, no ``PermissionRequest`` hook is registered and
        Claude falls back to its built-in TUI permission prompt.
    :param ap_auth_headers: Headers to send with the
        ``PermissionRequest`` command hook, e.g.
        ``{"Authorization": "Bearer <token>"}``. Stored in the
        owner-only bridge directory instead of in hook argv.
    :param api_key_helper: Optional Claude Code ``apiKeyHelper``
        command from ucode state, e.g. ``"databricks auth token
        --host https://example.databricks.com ..."``.
    :returns: JSON-serializable Claude settings fragment.
    """
    python = python_executable or sys.executable
    # -I (isolated mode) prevents Python from adding the session's
    # working directory to sys.path, which would shadow the installed
    # omnigent package with a local checkout in the cwd (e.g. a
    # git worktree that has its own omnigent/ directory on a
    # different branch).
    command_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_hook",
        "--bridge-dir",
        str(bridge_dir),
    ]
    command = shlex.join(command_parts)
    hook = {"type": "command", "command": command}
    session_start_hook = {
        "type": "command",
        "command": command,
    }
    # ``MessageDisplay`` fires once per streamed assistant-text chunk and
    # Claude blocks on the hook, so it gets a dedicated stdlib-only
    # appender module instead of the heavier observer ``hook`` above —
    # the per-chunk subprocess must stay cheap. It just appends the
    # chunk to ``<bridge_dir>/message_deltas.jsonl``; the forwarder tails
    # that file and publishes ``response.output_text.delta`` events.
    message_display_command_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_message_display_hook",
        "--bridge-dir",
        str(bridge_dir),
    ]
    message_display_hook = {
        "type": "command",
        "command": shlex.join(message_display_command_parts),
    }
    hooks: dict[str, Any] = {
        "SessionStart": [{"hooks": [session_start_hook]}],
        "Stop": [{"hooks": [hook]}],
        "StopFailure": [{"hooks": [hook]}],
        # ``UserPromptSubmit`` is the symmetric counterpart to
        # ``Stop`` — fires when a new user prompt reaches Claude
        # (web-UI message via tmux send-keys, or direct keystrokes
        # into the embedded terminal). The transcript forwarder
        # translates it into ``session.status: running``.
        "UserPromptSubmit": [{"hooks": [hook]}],
        # ``TaskCreated`` fires when Claude creates a new native task
        # (shown with ``□`` in the TUI). The payload carries ``task_id``
        # and ``task_subject``; the forwarder converts all current tasks
        # into a ``session.todos`` SSE event so the web UI can display
        # the task checklist.
        "TaskCreated": [{"hooks": [hook]}],
        # ``TaskCompleted`` fires when Claude marks a native task done
        # (``■`` in the TUI). The payload carries ``task_id`` so the
        # forwarder can flip that task's status to ``"completed"``.
        "TaskCompleted": [{"hooks": [hook]}],
        # ``PostToolUse`` filtered to ``TodoWrite`` fires whenever Claude
        # updates its simple todo list. The hook payload carries the new
        # todos under ``tool_input.todos``.
        # ``PostToolUse`` filtered to ``TaskUpdate`` fires when Claude
        # calls ``TaskUpdate`` to change a native task's status (e.g.
        # to ``"in_progress"``). The payload carries ``tool_input.taskId``
        # and ``tool_input.status``.
        "PostToolUse": [
            {"matcher": "TodoWrite", "hooks": [hook]},
            {"matcher": "TaskUpdate", "hooks": [hook]},
        ],
        # ``PreCompact`` fires right before Claude compacts its own
        # context — for both a manual ``/compact`` (web-UI button or
        # typed) and an automatic context-overflow compaction. The
        # forwarder translates it into a
        # ``response.compaction.in_progress`` SSE so the web UI shows
        # its "Compacting conversation…" spinner while Claude runs the
        # real compaction in the terminal. The matching completion
        # signal is ``SessionStart`` with ``source == "compact"`` (no
        # dedicated PreCompact-done hook exists), already wired above.
        "PreCompact": [{"hooks": [hook]}],
        # ``MessageDisplay`` fires per streamed assistant-text chunk.
        # Routed to the dedicated fast appender so the forwarder can
        # publish live token deltas to the web UI.
        "MessageDisplay": [{"hooks": [message_display_hook]}],
    }
    if ap_server_url:
        _write_json_file(
            bridge_dir / _PERMISSION_HOOK_FILE,
            {
                "ap_server_url": ap_server_url,
                "ap_auth_headers": ap_auth_headers or {},
                "updated_at": time.time(),
            },
        )
        # ``PermissionRequest`` fires only when Claude is about to
        # show its TUI permission prompt — that's exactly the
        # interception point we want for routing to the web UI.
        # Route through a command hook instead of baking a session id
        # into an HTTP URL at Claude launch. The subprocess reads the
        # current active session from bridge.json for every permission
        # request, so approvals follow `/clear` rotations without
        # restarting Claude.
        permission_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "permission-request",
            "--bridge-dir",
            str(bridge_dir),
        ]
        permission_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(permission_command_parts),
            # Wait up to a day for the verdict. Claude Code's default
            # command-hook timeout (~60s) would otherwise kill the hook
            # subprocess long before the user answers, putting the
            # prompt back in the TUI and flipping the web card to
            # "Resolved elsewhere". A day is effectively wait-forever
            # for an interactive permission prompt; it stays in lockstep
            # with the subprocess/AP-side budgets so none caps first.
            "timeout": 86400,
        }
        hooks["PermissionRequest"] = [{"hooks": [permission_hook]}]

        # Policy-gate native Claude Code tools, not just relay/MCP tools.
        evaluate_policy_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ]
        evaluate_policy_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(evaluate_policy_command_parts),
        }
        # In bypassPermissions mode PermissionRequest never fires, so
        # AskUserQuestion needs its own PreToolUse hook to surface the
        # form. It's a no-op in other modes to avoid double-surfacing.
        ask_uq_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "ask-user-question",
            "--bridge-dir",
            str(bridge_dir),
        ]
        ask_uq_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(ask_uq_command_parts),
            # Short timeout: if the web-UI elicitation isn't answered
            # within 10s, the hook returns empty output so Claude falls
            # through to its TUI picker in bypassPermissions mode. In
            # default mode this hook exits immediately (no-op), so the
            # timeout is irrelevant there.
            "timeout": 10,
        }
        hooks["PreToolUse"] = [
            {"matcher": "AskUserQuestion", "hooks": [ask_uq_hook]},
            {"hooks": [evaluate_policy_hook]},
        ]
        # PostToolUse already has TodoWrite and TaskUpdate matchers
        # for the transcript forwarder (the observer ``hook``). Append
        # a catch-all policy evaluation entry so TOOL_RESULT policies
        # fire for all tools, not just the forwarder-specific ones.
        hooks["PostToolUse"].append({"hooks": [evaluate_policy_hook]})
        # UserPromptSubmit already carries the transcript forwarder's
        # status hook (running). Append the policy hook so REQUEST-phase
        # policies gate native prompts — for native sessions this is the
        # sole request gate (the server-level ``_evaluate_input_policy``
        # skips native message events). A DENY emits ``decision: "block"``,
        # dropping the prompt before the model sees it; ASK is resolved
        # server-side. Covers both web-UI-injected and direct-terminal
        # prompts, since both fire UserPromptSubmit.
        hooks["UserPromptSubmit"].append({"hooks": [evaluate_policy_hook]})
    settings: dict[str, Any] = {"hooks": hooks}
    if api_key_helper:
        settings["apiKeyHelper"] = api_key_helper
    # Override Claude Code's statusLine so we receive its stdin (the
    # only place ``context_window`` surfaces). Chain to whatever the
    # user had globally so claude-hud / their bar still renders.
    status_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_status",
        "--bridge-dir",
        str(bridge_dir),
    ]
    chain_command = read_user_status_line_command()
    if chain_command is not None:
        status_parts.extend(["--chain", chain_command])
    settings["statusLine"] = {"type": "command", "command": shlex.join(status_parts)}
    return settings

def read_transcript_path(bridge_dir: Path) -> Path | None:
    """
    Return the transcript path last reported by Claude hooks.

    :param bridge_dir: Bridge directory path.
    :returns: Transcript path, or ``None`` when hooks have not
        reported one yet.
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    raw = state.get("transcript_path") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)

def read_claude_session_id(bridge_dir: Path) -> str | None:
    """
    Return the Claude-native session id captured from hook events.

    Set by :func:`record_hook_event` whenever a hook payload carries
    a ``session_id`` field (every event Claude Code emits does).
    Wrapper code reads it back to mirror the value into AP-side
    conversation state (e.g. ``external_session_id`` on the
    ``conversations`` row) so ``--resume`` can recover the prior
    Claude transcript on a fresh runner without the user having to
    know Claude's own id.

    :param bridge_dir: Bridge directory path.
    :returns: Claude session uuid string,
        e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``, or ``None``
        when no hook has yet reported one (the first poll after a
        cold launch).
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    raw = state.get("claude_session_id") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    return raw

def read_seen_claude_session_ids(bridge_dir: Path) -> set[str]:
    """
    Return Claude session ids already observed by this bridge.

    The set is transient local bridge state. It lets the hook
    distinguish Claude-created branch/fork session switches from
    ordinary resumes into sessions the wrapper already saw.

    :param bridge_dir: Bridge directory path.
    :returns: Claude session uuid strings, e.g.
        ``{"a1b2c3d4-1234-5678-9abc-def012345678"}``.
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    if not isinstance(state, dict):
        return set()
    seen: set[str] = set()
    raw_seen = state.get("seen_claude_session_ids")
    if isinstance(raw_seen, list):
        seen.update(value for value in raw_seen if isinstance(value, str) and value)
    raw_current = state.get("claude_session_id")
    if isinstance(raw_current, str) and raw_current:
        seen.add(raw_current)
    return seen

def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """
    Advertise the tmux socket + target for the Claude terminal.

    The runner calls this after launching the ``claude/main`` terminal
    so the harness can shell out to ``tmux send-keys`` against the
    same private socket the terminal was launched on.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param socket_path: Absolute path to the terminal's private tmux
        socket, e.g. ``Path("/tmp/.../tmux.sock")``.
    :param tmux_target: tmux pane target string, e.g. ``"claude:0.0"``.
    :param pid: Optional Claude process pid, recorded for diagnostics.
    :returns: None.
    """
    _ensure_secure_dir(bridge_dir)
    payload: dict[str, Any] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    _write_json_file(bridge_dir / _TMUX_FILE, payload)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
    for _key, _value in _sib_args.__dict__.items():
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
