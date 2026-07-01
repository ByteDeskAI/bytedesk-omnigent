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

def _mark_startup_step(
    startup_profiler: StartupProfiler,
    label: str,
    *,
    startup_progress: RunnerStartupProgress | None = None,
    progress_message: str | None = None,
    detail: str | None = None,
) -> None:
    """
    Record a startup phase for diagnostics and optional user progress.

    :param startup_profiler: Profiler receiving timing marks.
    :param label: Short phase label, e.g. ``"creating daemon claude session"``.
    :param startup_progress: Optional active progress renderer. ``None``
        means the phase is only recorded in the profiler.
    :param progress_message: Optional user-facing progress message,
        e.g. ``"Creating Claude session..."``. ``None`` keeps this
        mark out of the normal startup display.
    :param detail: Optional profiler detail, e.g. ``"runner=runner_abc123"``.
    :returns: None.
    """
    startup_profiler.mark(label, detail=detail)
    if startup_progress is not None and progress_message is not None:
        startup_progress.update(progress_message)

def _run_with_local_server(
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    claude_args: tuple[str, ...],
    command: str,
    claude_config: ClaudeNativeUcodeConfig | None = None,
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Start a local Omnigent server, launch Claude, and attach to it.

    :param spec_path: Generated Claude wrapper agent spec.
    :param session_id: Optional existing session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the picker.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param claude_config: Optional ucode-derived Claude Code config.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: None.
    """
    from omnigent.chat import (
        _bundle_agent,
        _find_free_port,
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    startup_profiler.mark("local server selecting port")
    port = _find_free_port()
    startup_profiler.mark("local server port selected", detail=f"port={port}")
    server_handle = _start_local_server(spec_path, port, ephemeral=False)
    startup_profiler.mark("local server process started")
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(port, server_handle)
        startup_profiler.mark("local server healthy")
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers={},
            session_id=session_id,
            resume_picker=resume_picker,
        )
        startup_profiler.mark(
            "session resolved",
            detail="fresh" if resolved_session_id is None else "resume",
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            # Picker cancelled — exit before creating a session the user declined.
            return
        if resolved_session_id is not None:
            # Resume path: bring the wrapper's cwd in line with the
            # session's recorded launch cwd BEFORE the bundle / runner
            # / terminal-launch steps sample ``Path.cwd()``. Local
            # server is already up at this point but is cwd-
            # independent (writes to ``~/.omnigent/``), so chdiring
            # now is safe.
            _align_working_directory_with_session(
                resolved_session_id,
                base_url=base_url,
                headers={},
            )
            startup_profiler.mark("resume workspace aligned")
        with runner_startup_progress(initial_message="Preparing Claude...") as progress:
            _mark_startup_step(
                startup_profiler,
                "bundling local agent",
                startup_progress=progress,
            )
            bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
            _mark_startup_step(
                startup_profiler,
                "local agent bundle ready",
                startup_progress=progress,
            )
            _mark_startup_step(
                startup_profiler,
                "preparing local terminal",
                startup_progress=progress,
            )
            prepared = asyncio.run(
                _prepare_claude_terminal(
                    base_url=base_url,
                    headers={},
                    session_id=resolved_session_id,
                    runner_id=server_handle.runner_id,
                    session_bundle=bundle,
                    claude_args=claude_args,
                    command=command,
                    claude_config=claude_config,
                    startup_profiler=startup_profiler,
                    startup_progress=progress,
                )
            )
            _mark_startup_step(
                startup_profiler,
                "local terminal prepared",
                startup_progress=progress,
                detail=_tmux_profile_detail(prepared),
            )
        if resolved_session_id is None:
            # Fresh-session path: now that the server has assigned a
            # conv id, persist the cwd we used at create time so a
            # future ``--resume`` can detect mismatches.
            _record_launch_for_fresh_session(prepared.session_id)
            startup_profiler.mark("fresh session launch state recorded")
        click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
        startup_profiler.mark("web ui url printed")
        open_conversation_link_if_enabled(
            base_url=base_url,
            conversation_id=prepared.session_id,
            enabled=auto_open_conversation,
            warn=lambda message: click.echo(message, err=True),
        )
        startup_profiler.mark("opening terminal attach")
        asyncio.run(
            _attach_with_transcript_forwarder(
                base_url=base_url,
                headers={},
                prepared=prepared,
                agent_name=_AGENT_NAME,
                attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
                attach=attach_local_terminal,
                startup_profiler=startup_profiler,
            )
        )
        if resolved_session_id is None:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
            )
    finally:
        _stop_local_server(server_handle)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
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
    for _key, _value in _sib_cwd.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
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
