"""Native Claude Code terminal wrapper for the Omnigent CLI.

The wrapper deliberately treats Claude Code as a terminal-first
program. It creates or binds an Omnigent session, launches ``claude``
through the existing runner terminal resource API, then attaches the
local TTY to the existing terminal WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sys
import termios
import tty
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry
    from omnigent.spec.types import AgentSpec

import click
import httpx
import yaml
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, WebSocketException
from websockets.frames import Close

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._startup_profile import StartupProfiler
from omnigent._terminal_picker_theme import (
    PICKER_ACCENT as _PICKER_ACCENT,
)
from omnigent._terminal_picker_theme import (
    PICKER_MUTED as _PICKER_MUTED,
)
from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY,
)
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    augment_claude_args,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_active_session_id,
    read_user_effort_level,
    url_component,
)
from omnigent.claude_native_forwarder import (
    reset_transcript_forward_state,
    supervise_forwarder,
)
from omnigent.claude_native_state import (
    read_launch_state,
    redirect_launch_state,
    write_launch_state,
)
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    DAEMON_POLL_INTERVAL_S,
    error_text,
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import (
    DAEMON_HOST_ONLINE_TIMEOUT_S as _DAEMON_HOST_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_RUNNER_ONLINE_TIMEOUT_S as _DAEMON_RUNNER_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_TERMINAL_READY_TIMEOUT_S as _DAEMON_TERMINAL_READY_TIMEOUT_S,
)
from omnigent.native_terminal import (
    bind_session_runner as _bind_session_runner,
)
from omnigent.native_terminal import (
    terminal_attach_url as _attach_url,
)
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)

_logger = logging.getLogger(__name__)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """
    Translate the CLI's resume inputs into a concrete session id.

    The picker is scoped to claude-native conversations.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers; ``{}`` for local server.
    :param session_id: Explicit ``--resume <id>``; wins over the picker.
    :param resume_picker: ``True`` for bare ``--resume`` (no value).
    :returns: Conversation id, or ``None`` for "start fresh" / picker cancelled.
    :raises click.ClickException: Picker requested but no prior sessions exist.
    """
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    # Deferred — omnigent_client / repl pull in heavy graphs we don't want at startup.
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
        async with OmnigentClient(
            base_url=base_url, headers=headers if headers else None
        ) as client:
            return await pick_conversation_by_wrapper_label_from_sdk(
                client, wrapper_value=_WRAPPER_LABEL_VALUE, agent_name=_AGENT_NAME
            )

    return asyncio.run(_drive())

def _align_working_directory_with_session(
    session_id: str,
    *,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """
    Resolve cwd mismatch before resuming a Claude-native session.

    Claude Code's ``--resume <claude_sid>`` (which the cold-resume
    path injects -- see :func:`_resolve_cold_resume_args`) requires
    the cwd of the resumed invocation to match the cwd of the
    original session. If they differ, Claude exits immediately on
    launch. The wrapper records the launch cwd in client-side
    persistent state at session creation (see
    :mod:`omnigent.claude_native_state`); this helper reads it back
    on resume and asks the user whether to switch cwd, move Claude's
    transcript into the current cwd, or leave without resuming.

    The state is **client-side and per-user**, not server-side, so:

    * A user resuming on the same machine they created the session
      on gets the chdir prompt; this is the common path.
    * A user resuming from a different machine has no recorded
      state for this conv id locally -- the helper silently
      proceeds (no prompt) and Claude will likely exit, at which
      point the user knows to start a fresh session. The wrapper
      cannot fabricate the cwd; only the original client knew it.

    Decision table:

    - **No state recorded**: silent no-op. Either a legacy session
      created before this tracking landed, or a session created on
      a different machine. Echoing a hint here would be noisy on
      every legacy resume; the user finds out via Claude's own
      "session not found / cwd mismatch" message if it matters.
    - **Recorded cwd matches current cwd**: silent no-op.
    - **Recorded cwd differs, recorded path exists**: offer
      ``switch`` (default), ``move``, or ``leave``. ``switch``
      mutates process cwd. ``move`` copies Claude's transcript
      into the current cwd's Claude project directory and updates the
      client-side launch state. ``leave`` cancels the resume before
      Claude can crash on the cwd mismatch.
    - **Recorded cwd differs, recorded path missing**: offer
      ``move`` when the Claude transcript can still be found;
      otherwise fail loud with a :class:`click.ClickException`.

    :param session_id: Resolved Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param base_url: Omnigent server base URL used to look up Claude's
        external session id for redirect, e.g. ``"http://127.0.0.1:6767"``.
        ``None`` means redirect is unavailable.
    :param headers: HTTP auth headers for *base_url*, e.g.
        ``{"Authorization": "Bearer ..."}``. ``None`` is treated as
        no headers.
    :returns: None. Side-effect-only -- the cwd is mutated when
        the user chooses ``switch``; Claude state is moved when the
        user chooses ``move``.
    :raises click.ClickException: When no viable switch or move path
        exists, when moving fails, or when the user leaves.
    """
    state = read_launch_state(session_id)
    if state is None:
        return
    current = Path.cwd().resolve()
    recorded_path = Path(state.working_directory).resolve()
    if current == recorded_path:
        return
    external_session_id = _fetch_external_session_id_for_redirect(
        base_url=base_url,
        headers=headers or {},
        session_id=session_id,
    )
    redirect_available = _redirect_available(external_session_id)
    if not recorded_path.is_dir() and not redirect_available:
        raise click.ClickException(
            f"Session {session_id} was created in {recorded_path}, but that "
            f"directory no longer exists and Claude transcript "
            f"{external_session_id or '<unknown>'!r} was not found locally. "
            f"Recreate or move the project back before resuming."
        )
    action = _prompt_resume_workspace_action(
        recorded_path=recorded_path,
        current=current,
        redirect_available=redirect_available,
    )
    if action == _RESUME_ACTION_SWITCH:
        _switch_to_recorded_working_directory(recorded_path)
        return
    if action == _RESUME_ACTION_MOVE:
        if external_session_id is None:
            raise click.ClickException(
                "Cannot move Claude transcript: no external session id was found."
            )
        _redirect_claude_transcript_to_current_project(
            session_id=session_id,
            external_session_id=external_session_id,
            current=current,
        )
        return
    raise click.ClickException("Resume cancelled.")

def _switch_to_recorded_working_directory(recorded_path: Path) -> None:
    """
    Switch process cwd to *recorded_path* for Claude resume.

    :param recorded_path: Existing recorded launch cwd.
    :returns: None.
    """
    os.chdir(recorded_path)
    click.echo(f"Switched to {recorded_path}.", err=True)

def _record_launch_for_fresh_session(session_id: str) -> None:
    """
    Persist the wrapper's current cwd as the session's launch state.

    Called on the fresh-session path after
    :func:`_prepare_claude_terminal` returns a new conversation id
    but before attaching the terminal. The recorded value drives
    the resume-time chdir prompt on subsequent invocations.

    Best-effort: a failed write is logged and swallowed. The launch
    state is a UX nicety, not a correctness primitive -- a single-
    inode write failure shouldn't crash the wrapper between session
    creation and attach (the user would be left with a usable
    session they can't terminate cleanly). The fallout from a
    missing record is just "no chdir prompt on resume", which is
    the same as a legacy session.

    :param session_id: Newly created Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        write_launch_state(session_id, str(Path.cwd().resolve()))
    except OSError:
        # File-system error (read-only fs, disk full, permission
        # denied). Log and proceed -- attach still works; the user
        # just won't get the chdir prompt on resume.
        _logger.warning(
            "failed to record launch state for %s",
            session_id,
            exc_info=True,
        )

def _strip_resume_from_claude_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """
    Strip any stray ``--resume`` / ``-r`` (and value) from raw args.

    Defense in depth: a user routing ``--resume`` past Click (e.g.
    ``omnigent claude -- --resume <id>``) must not have it reach
    upstream Claude, which would apply it to its own session-id
    namespace.

    :param args: Raw ``claude_args`` from Click pass-through.
    :returns: Args with stray ``--resume`` / ``-r`` removed.
    """
    out: list[str] = []
    consume_value = False
    for arg in args:
        if consume_value:
            consume_value = False
            # Only swallow the next token if it looks like a value, not a flag.
            # ``-- --resume --foo`` should drop ``--resume`` but keep ``--foo``.
            if not arg.startswith("-"):
                _logger.warning("Stripped stray --resume value %r from claude args.", arg)
                continue
            out.append(arg)
            continue
        if arg in ("--resume", "-r"):
            _logger.warning(
                "Stripped stray %s from claude args; use `omnigent claude --resume`.", arg
            )
            consume_value = True
            continue
        if arg.startswith(("--resume=", "-r=")):
            _logger.warning("Stripped stray %s from claude args.", arg.split("=", 1)[0])
            continue
        out.append(arg)
    return tuple(out)

def _preflight_local_tools(command: str) -> None:
    """
    Verify local executables required by the native Claude wrapper.

    :param command: Claude executable to run locally, e.g.
        ``"claude"``.
    :returns: None when the local runner can launch Claude.
    :raises click.ClickException: If ``command`` or ``tmux`` is not
        available on the local ``PATH``.
    """
    if shutil.which(command) is None:
        raise click.ClickException(
            f"Claude Code CLI command {command!r} was not found on local PATH. "
            "--server selects the Omnigent server only; Claude still runs locally."
        )
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Claude wrapper "
            "launches Claude through the local runner's tmux terminal."
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _terminal as _sib_terminal
    from . import _transcript as _sib_transcript
    from . import _types as _sib_types
    for _key, _value in _sib_cold_resume.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_config.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_resume_ui.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
