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

def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    claude_args: tuple[str, ...],
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Launch Claude on a remote Omnigent server via the connect daemon.

    Ensures the connect daemon is running for *base_url*, then routes
    the runner launch through it (HOST_BY_DEFAULT): the daemon — not
    this CLI — spawns the runner, which brings the Claude terminal up
    itself (applying the persisted launch args, model, cold resume, and
    the ucode gateway auth from the provider config). The CLI
    creates/resolves the session, persists the pass-through args, waits
    for the daemon-spawned runner + its auto-created terminal, and
    attaches (directly to the runner's tmux when it is local, else over
    the WebSocket PTY bridge). See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param base_url: Remote Omnigent server base URL without a trailing
        slash, e.g. ``"https://example.databricks.com"``.
    :param spec_path: Generated Claude wrapper agent spec.
    :param session_id: Optional existing session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the picker.
    :param claude_args: Claude CLI args, persisted on the session as
        ``terminal_launch_args`` for the runner to apply. (The runner
        launches ``claude`` itself and derives the ucode config from the
        provider config, so this path takes neither a ``command`` nor a
        ``claude_config``.)
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after the session is prepared.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: None.
    """
    from omnigent.chat import _bundle_agent, _remote_headers, _server_auth
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    startup_profiler.mark("remote headers resolving")
    headers = _remote_headers(server_url=base_url)
    startup_profiler.mark("remote headers resolved")
    # ``headers`` carries the bearer for the WebSocket attach handshake
    # (refreshed in place by ``_recover``). For HTTP requests we additionally
    # supply an ``httpx.Auth`` that mints a fresh token per request, so the
    # long-lived transcript-forwarder client survives the ~1h Databricks
    # OAuth token TTL.
    startup_profiler.mark("remote auth resolving")
    forwarder_auth = _server_auth(server_url=base_url)
    startup_profiler.mark("remote auth resolved")
    prepared: PreparedClaudeTerminal | None = None
    # Bound before the attach call so the ``finally`` can read it even
    # if setup raises early; only a real tmux detach flips it.
    outcome = _AttachOutcome.EXITED
    attach_completed = False
    should_print_resume_hint = False
    try:
        startup_profiler.mark("resolving remote session")
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            resume_picker=resume_picker,
        )
        startup_profiler.mark(
            "remote session resolved",
            detail="fresh" if resolved_session_id is None else "resume",
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            # Picker cancelled — don't launch a runner or fresh session.
            return
        should_print_resume_hint = resolved_session_id is None
        with runner_startup_progress(initial_message="Preparing Claude...") as progress:
            if resolved_session_id is not None:
                # Align cwd with the resumed session before we sample
                # ``Path.cwd()`` for the runner workspace below.
                _align_working_directory_with_session(
                    resolved_session_id,
                    base_url=base_url,
                    headers=headers,
                )
                _mark_startup_step(
                    startup_profiler,
                    "remote resume workspace aligned",
                    startup_progress=progress,
                )

            # Ensure the connect daemon is up for this server, then route the
            # runner launch through it. The runner the daemon spawns brings
            # up the Claude terminal itself, so the CLI just waits and
            # attaches.
            _mark_startup_step(
                startup_profiler,
                "ensuring host daemon",
                startup_progress=progress,
                progress_message="Connecting to local daemon...",
            )
            _ensure_host_daemon(base_url)
            _mark_startup_step(
                startup_profiler,
                "host daemon ready",
                startup_progress=progress,
            )
            host_id = load_or_create_host_identity().host_id
            _mark_startup_step(
                startup_profiler,
                "host identity loaded",
                startup_progress=progress,
                detail=f"host={host_id}",
            )

            _mark_startup_step(
                startup_profiler,
                "bundling remote agent",
                startup_progress=progress,
            )
            bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
            _mark_startup_step(
                startup_profiler,
                "remote agent bundle ready",
                startup_progress=progress,
            )
            try:
                _mark_startup_step(
                    startup_profiler,
                    "preparing daemon terminal",
                    startup_progress=progress,
                )
                prepared = asyncio.run(
                    _prepare_claude_terminal_via_daemon(
                        base_url=base_url,
                        headers=headers,
                        session_id=resolved_session_id,
                        session_bundle=bundle,
                        claude_args=claude_args,
                        host_id=host_id,
                        workspace=str(Path.cwd().resolve()),
                        startup_profiler=startup_profiler,
                        startup_progress=progress,
                    )
                )
                _mark_startup_step(
                    startup_profiler,
                    "daemon terminal prepared",
                    startup_progress=progress,
                    detail=_tmux_profile_detail(prepared),
                )
            except httpx.ConnectError as exc:
                # The first server contact (session create) could not open a
                # TCP connection — the Omnigent server at this URL isn't reachable.
                # Fail loud with the URL instead of a raw httpx traceback.
                raise click.ClickException(
                    f"Could not reach the omnigent server at {base_url}. "
                    "Confirm the server is running and reachable from here "
                    f"(e.g. `curl {base_url}/health`), and that --server is correct."
                ) from exc
        if resolved_session_id is None:
            _record_launch_for_fresh_session(prepared.session_id)
            startup_profiler.mark("fresh remote launch state recorded")
        click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
        startup_profiler.mark("remote web ui url printed")
        open_conversation_link_if_enabled(
            base_url=base_url,
            conversation_id=prepared.session_id,
            enabled=auto_open_conversation,
            warn=lambda message: click.echo(message, err=True),
        )

        async def _recover() -> None:
            """
            Refresh the bearer in place between attach attempts.

            The daemon owns the runner lifecycle now, so — unlike the
            old CLI-spawned path — recovery does not restart a runner. It
            only re-resolves the Databricks bearer and mutates the shared
            *headers* dict in place so a reconnect after a server bounce
            or token expiry handshakes with a fresh token. If the
            daemon-spawned runner died, the server relaunches it on the
            next message (host-bound auto-relaunch).
            """
            new_headers = _remote_headers(server_url=base_url)
            headers.clear()
            headers.update(new_headers)

        startup_profiler.mark("opening remote terminal attach")
        outcome = asyncio.run(
            _attach_with_transcript_forwarder(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                agent_name=_AGENT_NAME,
                attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
                attach=attach_local_terminal,
                recover=_recover,
                auth=forwarder_auth,
                run_transcript_forwarder=False,
                startup_profiler=startup_profiler,
            )
        )
        attach_completed = True
    finally:
        # The daemon owns the runner — the CLI no longer adopts or stops
        # it. On detach the session keeps running for the web UI; on a
        # clean exit the server idle-reaps the runner.
        if prepared is not None and outcome is _AttachOutcome.DETACHED:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            click.echo(
                f"\nDetached. Agent still running at "
                f"{conversation_url(base_url, active_session_id)}",
                err=True,
            )
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
                server=base_url,
            )
        elif prepared is not None and attach_completed and should_print_resume_hint:
            # Reached only when the attach did NOT detach (the ``if``
            # above handled DETACHED), so this is a clean fresh-session
            # exit — print the resume command for next time.
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
                server=base_url,
            )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
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
    for _key, _value in _sib_local_server.__dict__.items():
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
