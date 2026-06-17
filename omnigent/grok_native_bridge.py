"""Bridge utilities for native Grok TUI sessions.

The Grok Build CLI (``grok``, npm ``@xai-official/grok``) spawns a *leader*
daemon when the bare ``grok`` TUI starts.  The leader listens on a Unix socket
(default ``~/.grok/leader.sock``) and acts as the session owner.  A separate
``grok agent stdio --leader-socket <path>`` process can attach to that leader,
discover its resident session via the ``_x.ai/sessions/changed`` ACP
notification, and inject prompts that render in the running TUI.

This module provides:
- Env-var constants shared between the runner (which sets them) and the
  harness executor (which reads them).
- A minimal ``state.json`` persistence layer for the leader-discovered session
  id so the executor can reconnect across turn boundaries without re-scanning.
- A ``build_grok_native_spawn_env`` helper consumed by ``runner/app.py`` at
  harness-spawn time — exactly the same pattern as
  ``omnigent.codex_native_bridge.build_codex_native_spawn_env``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# ── Env-var names (runner → executor) ─────────────────────────────────────

#: Path to the grok leader Unix socket.  Set by the runner when it creates a
#: grok TUI terminal so the executor can attach without hard-coding the path.
GROK_NATIVE_LEADER_SOCKET_ENV_VAR = "HARNESS_GROK_LEADER_SOCKET"

#: Directory for per-conversation bridge state (discovered session id etc.).
GROK_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_GROK_NATIVE_BRIDGE_DIR"

#: Omnigent conversation id for the running harness process.
GROK_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_GROK_NATIVE_REQUEST_SESSION_ID"

_STATE_FILE = "state.json"
_TMUX_FILE = "tmux.json"
_BRIDGE_ROOT = Path.home() / ".omnigent" / "grok-native"

# Default leader socket path used when not overridden by env.
GROK_DEFAULT_LEADER_SOCKET = Path.home() / ".grok" / "leader.sock"


def grok_leader_socket_for_session(session_id: str) -> Path:
    """
    Return a **per-conversation** grok leader socket path.

    Each conversation's grok TUI auto-spawns its own leader at this path (gated
    by ``[cli] use_leader`` in config.toml), and the harness executor attaches
    to the same path.  Isolating the socket per conversation means the executor
    only ever sees *its own* TUI's resident session in ``_x.ai/sessions/changed``
    — no cross-wiring when several conversations run on one host.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: ``~/.grok/leader-<digest>.sock``.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".grok" / f"leader-{digest}.sock"


# ── State dataclass ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GrokNativeBridgeState:
    """
    Runtime state shared by the native Grok TUI wrapper and harness.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param grok_session_id: Grok ACP session id discovered from the leader,
        e.g. ``"sess_abc123"``.
    :param leader_socket: Unix socket path the executor should attach to,
        e.g. ``"/home/user/.grok/leader.sock"``.
    """

    session_id: str
    grok_session_id: str
    leader_socket: str


# ── Bridge directory helpers ───────────────────────────────────────────────


def bridge_root() -> Path:
    """Return the configured Grok-native bridge root."""
    return _BRIDGE_ROOT


def bridge_dir_for_session_id(session_id: str) -> Path:
    """
    Return the bridge directory for a native Grok session.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Absolute bridge directory under ``~/.omnigent/grok-native``.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(session_id: str) -> Path:
    """
    Create and return the bridge directory for *session_id*.

    :param session_id: Omnigent conversation id.
    :returns: Prepared absolute bridge directory.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    return bridge_dir


# ── Spawn env (runner → executor) ─────────────────────────────────────────


def build_grok_native_spawn_env(
    session_id: str,
    *,
    leader_socket: Path | str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``grok-native`` harness process.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param leader_socket: Path to the grok leader Unix socket.  ``None``
        defaults to :data:`GROK_DEFAULT_LEADER_SOCKET`.
    :returns: Environment variables needed by the grok-native harness executor.
    """
    resolved_socket = str(leader_socket or grok_leader_socket_for_session(session_id))
    bridge_dir = prepare_bridge_dir(session_id)
    return {
        GROK_NATIVE_LEADER_SOCKET_ENV_VAR: resolved_socket,
        GROK_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir),
        GROK_NATIVE_REQUEST_SESSION_ID_ENV_VAR: session_id,
    }


# ── State persistence ──────────────────────────────────────────────────────


def write_bridge_state(bridge_dir: Path, state: GrokNativeBridgeState) -> None:
    """
    Persist discovered native Grok bridge state atomically.

    :param bridge_dir: Grok native bridge directory.
    :param state: State payload to persist.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _STATE_FILE
    payload = {
        "session_id": state.session_id,
        "grok_session_id": state.grok_session_id,
        "leader_socket": state.leader_socket,
    }
    fd, tmp = tempfile.mkstemp(prefix=f"{_STATE_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_bridge_state(bridge_dir: Path) -> GrokNativeBridgeState | None:
    """
    Read native Grok bridge state from *bridge_dir*.

    :param bridge_dir: Grok native bridge directory.
    :returns: Parsed state, or ``None`` when no state file exists.
    """
    path = bridge_dir / _STATE_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    grok_session_id = raw.get("grok_session_id")
    leader_socket = raw.get("leader_socket")
    if not all(isinstance(v, str) and v for v in (session_id, grok_session_id, leader_socket)):
        return None
    return GrokNativeBridgeState(
        session_id=session_id,
        grok_session_id=grok_session_id,
        leader_socket=leader_socket,
    )


def clear_bridge_state(bridge_dir: Path) -> None:
    """
    Remove stale bridge state for *bridge_dir*.

    :param bridge_dir: Grok native bridge directory.
    """
    try:
        (bridge_dir / _STATE_FILE).unlink()
    except FileNotFoundError:
        pass


# ── tmux target (runner → executor) for panel mirroring ────────────────────


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path | str,
    tmux_target: str,
) -> None:
    """
    Advertise the grok TUI terminal's tmux socket + pane target.

    The runner calls this after launching the per-conversation ``grok`` TUI so
    the harness executor can ``tmux send-keys`` the *first* user message into the
    TUI (which makes the TUI own + render a resident session).  Every later turn
    is delivered over ACP ``session/prompt`` to that resident session, which the
    TUI also renders — so send-keys is only the bootstrap.

    :param bridge_dir: Grok native bridge directory.
    :param socket_path: Absolute path to the terminal's private tmux socket.
    :param tmux_target: tmux pane target string, e.g. ``"main:0.0"``.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    fd, tmp = tempfile.mkstemp(prefix=f"{_TMUX_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, bridge_dir / _TMUX_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_tmux_target(bridge_dir: Path) -> dict[str, str] | None:
    """
    Read the advertised tmux target, or ``None`` if not yet published.

    :param bridge_dir: Grok native bridge directory.
    :returns: ``{"socket_path": ..., "tmux_target": ...}`` or ``None``.
    """
    path = bridge_dir / _TMUX_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sp, tt = raw.get("socket_path"), raw.get("tmux_target")
    if not (isinstance(sp, str) and isinstance(tt, str) and sp and tt):
        return None
    return {"socket_path": sp, "tmux_target": tt}


# How long to wait for the grok TUI's input box to render before pasting (the
# TUI cold-boots a few seconds — longer under pod CPU contention — and keys
# typed before the input box mounts are silently dropped), and how long to keep
# re-submitting Enter until the draft visibly leaves the box.
_TUI_READY_TIMEOUT_S = 60.0
_TUI_SUBMIT_VERIFY_S = 8.0
_TUI_POLL_S = 0.4


def _grok_capture(socket_path: str, target: str) -> str:
    """Return the rendered TUI pane text, or ``""`` on failure."""
    try:
        return subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except subprocess.SubprocessError:
        return ""


def _grok_prompt_ready(pane: str) -> bool:
    """True once the grok composer/input box has rendered (footer is stable)."""
    return ("Grok Composer" in pane) or ("Shift+Tab" in pane) or ("❯" in pane)


def _grok_draft_present(pane: str, needle: str) -> bool:
    """True if *needle* still sits on the ``❯`` input line (not yet submitted)."""
    for line in pane.splitlines():
        if "❯" in line and needle and needle in line:
            return True
    return False


def inject_user_message(bridge_dir: Path, *, content: str, timeout_s: float = 30.0) -> bool:
    """
    Type *content* into the grok TUI pane via tmux, to bootstrap its session.

    Waits for the runner to advertise ``tmux.json`` and then for the TUI's input
    box to render (a freshly-launched TUI drops keys typed before it mounts).
    Delivered as a bracketed paste (``load-buffer`` + ``paste-buffer -p``) so
    interior newlines ride as data rather than submitting per-line; ``Enter`` is
    a separate, *verified* submit — re-sent until the draft leaves the box.

    :param bridge_dir: Grok native bridge directory the runner published to.
    :param content: User text to deliver (non-empty).
    :param timeout_s: Seconds to wait for the runner to advertise ``tmux.json``.
    :returns: ``True`` on a delivered submit; ``False`` if no tmux target was
        advertised within *timeout_s* (caller falls back to a self-owned session).
    """
    deadline = time.monotonic() + timeout_s
    info: dict[str, str] | None = None
    while time.monotonic() < deadline:
        info = read_tmux_target(bridge_dir)
        if info is not None:
            break
        time.sleep(0.25)
    if info is None:
        return False
    socket_path, target = info["socket_path"], info["tmux_target"]

    def _tmux(*args: str) -> None:
        subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=True,
            capture_output=True,
            timeout=10,
        )

    # Wait for the input box to mount so the paste isn't dropped into a booting TUI.
    ready_deadline = time.monotonic() + _TUI_READY_TIMEOUT_S
    while time.monotonic() < ready_deadline:
        if _grok_prompt_ready(_grok_capture(socket_path, target)):
            break
        time.sleep(_TUI_POLL_S)

    # Clear any leftover draft, then paste the message.
    with contextlib.suppress(subprocess.SubprocessError):
        _tmux("send-keys", "-t", target, "C-a")
        _tmux("send-keys", "-t", target, "C-k")
    with tempfile.NamedTemporaryFile(
        dir=str(bridge_dir), prefix="paste_", suffix=".bin", delete=False
    ) as pf:
        pf.write((content + "\n").encode("utf-8"))
        paste_path = pf.name
    try:
        _tmux("load-buffer", "-b", "omnigent-grok-paste", paste_path)
        _tmux("paste-buffer", "-p", "-d", "-b", "omnigent-grok-paste", "-t", target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)

    # Submit, then verify the draft left the input box; re-Enter while it hasn't
    # (the TUI can fold an Enter that lands mid-paste into the draft as a newline).
    needle = "".join(content.split())[:24]
    time.sleep(_TUI_POLL_S)
    _tmux("send-keys", "-t", target, "Enter")
    verify_deadline = time.monotonic() + _TUI_SUBMIT_VERIFY_S
    while time.monotonic() < verify_deadline:
        time.sleep(_TUI_POLL_S)
        if not _grok_draft_present(_grok_capture(socket_path, target), needle):
            break
        with contextlib.suppress(subprocess.SubprocessError):
            _tmux("send-keys", "-t", target, "Enter")
    return True
