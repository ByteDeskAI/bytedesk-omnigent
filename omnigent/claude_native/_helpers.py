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

def _redirect_available(external_session_id: str | None) -> bool:
    """
    Return whether a Claude transcript can be redirected.

    :param external_session_id: Claude session id, or ``None``.
    :returns: ``True`` when a matching local transcript exists.
    """
    if external_session_id is None:
        return False
    return _find_claude_transcript(external_session_id) is not None

def _websocket_connect(attach_url: str, *, headers: dict[str, str]) -> Any:
    """
    Return a websockets connection context manager.

    The ``websockets`` package renamed the handshake header argument
    across releases. This compatibility wrapper keeps attach working
    across both supported versions.

    :param attach_url: Fully-qualified ``ws://`` or ``wss://`` URL.
    :param headers: Headers to send during the WebSocket handshake.
    :returns: Async context manager yielded by ``websockets.connect``.
    """
    import websockets

    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    # Identify as a first-party client so the server's WebSocket origin
    # guard (CSWSH protection) allows the handshake — this attach client
    # is not a browser. Set on a copy so the caller's dict (which also
    # carries auth headers and may be reused) is not mutated here.
    handshake_headers = {**headers, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
    try:
        return websockets.connect(
            attach_url,
            additional_headers=handshake_headers,
            close_timeout=_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S,
        )
    except TypeError:
        return websockets.connect(
            attach_url,
            extra_headers=handshake_headers,
            close_timeout=_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S,
        )

async def _stdin_to_websocket(
    ws: Any,
    stdin_fd: int,
    *,
    eof_event: asyncio.Event | None = None,
) -> None:
    """
    Copy local stdin bytes to the terminal WebSocket.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor.
    :param eof_event: Optional event set when the local stdin
        reaches EOF (i.e. the user closed the TTY input). Lets the
        outer attach loop distinguish a user-initiated exit from a
        server-initiated close.
    :returns: None on EOF or WebSocket close.
    """
    while True:
        data = await _read_fd(stdin_fd)
        if not data:
            if eof_event is not None:
                eof_event.set()
            await ws.close()
            return
        await ws.send(data)

async def _websocket_to_stdout(ws: Any, stdout_fd: int) -> None:
    """
    Copy terminal WebSocket bytes to local stdout.

    ``async for message in ws`` ends silently on any close, so the
    4404 "terminal gone" code never reaches the outer reconnect
    loop on its own. Surface that specific code as
    :class:`ConnectionClosedError`; other codes fall through to the
    outer loop's existing transient-close path (probe + backoff
    retry).

    :param ws: Connected ``websockets`` client.
    :param stdout_fd: Local stdout file descriptor.
    :returns: ``None`` on a transient close; never returns on 4404.
    :raises ConnectionClosedError: When the peer closed with
        :data:`WS_CLOSE_TERMINAL_NOT_FOUND`, so the outer loop's
        :func:`_is_terminal_not_found_close` check fires.
    """
    async for message in ws:
        if isinstance(message, str):
            continue
        await asyncio.to_thread(os.write, stdout_fd, bytes(message))
    close_code = getattr(ws, "close_code", None)
    if close_code == WS_CLOSE_TERMINAL_NOT_FOUND:
        raise ConnectionClosedError(
            Close(close_code, getattr(ws, "close_reason", None) or ""),
            None,
        )

async def _read_fd(fd: int) -> bytes:
    """
    Await one readable event on *fd* and return bytes from it.

    :param fd: File descriptor to read, e.g. ``0`` for stdin.
    :returns: Bytes read from the descriptor; ``b""`` means EOF.
    """
    loop = asyncio.get_running_loop()
    try:
        return await _read_fd_with_reader(loop, fd)
    except (NotImplementedError, RuntimeError):
        return await asyncio.to_thread(os.read, fd, 4096)

async def _read_fd_with_reader(loop: asyncio.AbstractEventLoop, fd: int) -> bytes:
    """
    Read *fd* using the event loop's reader callback API.

    :param loop: Running event loop.
    :param fd: File descriptor to read.
    :returns: Bytes read from *fd*.
    """
    fut: asyncio.Future[bytes] = loop.create_future()

    def _ready() -> None:
        """Complete the pending read future from the selector callback."""
        if fut.done():
            return
        try:
            fut.set_result(os.read(fd, 4096))
        except OSError as exc:
            fut.set_exception(exc)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)

    loop.add_reader(fd, _ready)
    try:
        return await fut
    finally:
        if not fut.done():
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)

async def _send_resize(ws: Any, stdin_fd: int) -> None:
    """
    Send the current local terminal size over the attach protocol.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor used for terminal
        size detection.
    :returns: None.
    """
    size = os.get_terminal_size(stdin_fd) if os.isatty(stdin_fd) else os.terminal_size((80, 24))
    await ws.send(json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines}))

def _enter_raw_mode(fd: int) -> list[Any] | None:
    """
    Put *fd* into raw mode when it is a TTY.

    :param fd: File descriptor to update.
    :returns: Previous termios attributes, or ``None`` when *fd* is
        not a TTY.
    """
    if not os.isatty(fd):
        return None
    old_attrs = termios.tcgetattr(fd)
    tty.setraw(fd)
    return old_attrs

def _restore_terminal(fd: int, old_attrs: list[Any] | None) -> None:
    """
    Restore termios attributes saved by :func:`_enter_raw_mode`.

    :param fd: File descriptor to restore.
    :param old_attrs: Attributes returned from
        :func:`_enter_raw_mode`.
    :returns: None.
    """
    if old_attrs is None:
        return
    with contextlib.suppress(termios.error, OSError):
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

@dataclass
class _SignalRestore:
    """
    Restore handle for attach-time signal handlers.

    :param restore: Callable that reinstalls previous handlers.
    :param stop_event: Event set when SIGTERM/SIGHUP arrives.
    :param received_signal: Last stop signal number, if any.
    """

    restore: Callable[[], None]
    stop_event: asyncio.Event
    received_signal: int | None = None

def _install_attach_signal_handlers(ws: Any, stdin_fd: int) -> _SignalRestore:
    """
    Install resize and stop signal handlers for local attach.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor.
    :returns: Restore handle for previous signal handlers.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    previous: dict[signal.Signals, Any] = {}
    resize_tasks: set[asyncio.Task[None]] = set()
    restore_handle = _SignalRestore(lambda: None, stop_event)

    def _request_stop(sig: signal.Signals) -> None:
        """Record *sig* and let the attach loop unwind normally."""
        restore_handle.received_signal = int(sig)
        stop_event.set()

    def _resize() -> None:
        """Forward SIGWINCH as an attach-protocol resize message."""
        task = asyncio.create_task(_send_resize(ws, stdin_fd))
        resize_tasks.add(task)
        task.add_done_callback(resize_tasks.discard)

    for sig, handler in {
        signal.SIGWINCH: _resize,
        signal.SIGTERM: lambda: _request_stop(signal.SIGTERM),
        signal.SIGHUP: lambda: _request_stop(signal.SIGHUP),
    }.items():
        previous[sig] = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            continue

    def _restore() -> None:
        """Restore handlers replaced by this attach session."""
        for sig, handler in previous.items():
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)

    restore_handle.restore = _restore
    return restore_handle

def claude_terminal_resource_id() -> str:
    """
    Return the deterministic terminal id used by ``omnigent claude``.

    :returns: Terminal resource id, e.g. ``"terminal_claude_main"``.
    """
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
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
    for _key, _value in _sib_cwd.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
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
