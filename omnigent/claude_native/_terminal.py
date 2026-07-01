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

def _tmux_profile_detail(prepared: PreparedClaudeTerminal) -> str:
    """
    Return profile detail for the terminal attach path.

    :param prepared: Prepared Claude terminal details.
    :returns: Human-readable attach-path detail, e.g.
        ``"direct-tmux target=main"`` or ``"websocket attach"``.
    """
    if (
        isinstance(prepared.tmux_socket, Path)
        and prepared.tmux_target is not None
        and _can_attach_direct_tmux(prepared)
    ):
        return f"direct-tmux target={prepared.tmux_target}"
    if prepared.tmux_socket is not None and prepared.tmux_target is not None:
        return "websocket attach (tmux socket not local)"
    return "websocket attach"

def _can_attach_direct_tmux(prepared: PreparedClaudeTerminal) -> bool:
    """
    Return whether this process can attach to the runner tmux directly.

    ``True`` only when the runner advertised a tmux socket + target, the
    socket exists on this host (so the runner shares this machine), and
    ``tmux`` is on PATH. This is the same-machine fast path: it wires the
    local TTY straight to the runner's tmux pane instead of relaying
    every keystroke over the WebSocket PTY bridge. A remote runner's
    socket won't exist locally, so this returns ``False`` and the caller
    falls back to the WebSocket attach. Mirrors the Codex wrapper's
    :func:`omnigent.codex_native._can_attach_direct_tmux`.

    :param prepared: Prepared terminal details.
    :returns: ``True`` when a direct local tmux attach is possible.
    """
    return (
        prepared.tmux_socket is not None
        and prepared.tmux_target is not None
        and prepared.tmux_socket.exists()
        and shutil.which("tmux") is not None
    )

async def _attach_direct_tmux(
    socket_path: Path,
    tmux_target: str,
    *,
    startup_profiler: StartupProfiler | None = None,
) -> _AttachOutcome:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    Lower latency than the WebSocket PTY relay because there is no
    server round-trip — the local TTY drives the runner's private tmux
    server over its Unix socket. ``TMUX`` is dropped from the child
    environment so a user who runs ``omnigent claude`` from inside
    their own tmux can still attach to Omnigent' server. After the
    ``tmux attach`` child exits, a ``has-session`` probe distinguishes a
    user *detach* (session still alive → keep the Omnigent terminal resource
    live) from Claude *exiting* (session gone → caller closes the
    resource), matching the WebSocket path's 4405-vs-4404 semantics.

    :param socket_path: Runner tmux server socket path.
    :param tmux_target: tmux ``-t`` target to attach, e.g. ``"main"``.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: :attr:`_AttachOutcome.DETACHED` when the tmux session
        outlives the attach (user detached), else
        :attr:`_AttachOutcome.EXITED`.
    """
    from omnigent.terminals.ws_bridge import _tmux_session_alive

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    env = dict(os.environ)
    env.pop("TMUX", None)
    startup_profiler.mark("starting tmux attach subprocess", detail=f"target={tmux_target}")
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-S",
        str(socket_path),
        "-f",
        os.devnull,
        "attach",
        "-t",
        tmux_target,
        env=env,
    )
    startup_profiler.mark("tmux attach subprocess started")
    await process.wait()
    startup_profiler.mark("tmux attach subprocess exited")
    if await _tmux_session_alive(str(socket_path), tmux_target):
        return _AttachOutcome.DETACHED
    return _AttachOutcome.EXITED

async def _attach_with_reconnect(
    *,
    attach: Callable[..., Any],
    attach_url: str,
    headers: dict[str, str],
    recover: Callable[[], Awaitable[None]] | None,
    base_url: str | None = None,
    session_id: str | None = None,
    terminal_id: str | None = None,
    bridge_dir: Path | None = None,
    active_session_id_reader: Callable[[], str | None] | None = None,
    close_attach_on_terminal_gone: bool = False,
) -> _AttachOutcome:
    """
    Attach to the terminal WebSocket, reconnecting on transient failures.

    The loop exits on user EOF, on SIGTERM/SIGHUP, on tmux detach
    (4405 close), or when the terminal is gone (4404 close, or
    post-close probe reports missing / not-running). Other outcomes —
    connection refused, abnormal close, clean close during a server
    bounce — back off and reattach. ``recover=None`` disables reconnect
    entirely (the local-server flow owns the server lifecycle and has
    nothing to reconnect to); the loop runs ``attach`` once and returns.

    :param attach: One attach attempt; signature
        ``(url, *, headers) -> bool``. ``True`` = user-requested
        exit, ``False`` = WS closed for any other reason. Runtime
        callable is :func:`attach_local_terminal`.
    :param attach_url: ``ws://`` / ``wss://`` terminal-attach URL.
    :param headers: WebSocket handshake headers. Mutated in place by
        ``recover`` so the next handshake sees the refreshed bearer;
        do not rebind.
    :param recover: Optional async callback invoked between attempts
        (not before the first). ``None`` disables reconnect; the
        loop returns after one ``attach`` call. Callback exceptions
        are logged and the loop still retries.
    :param base_url: Omnigent server URL for the post-close terminal probe;
        ``None`` disables the probe.
    :param session_id: Session/conversation id for the probe path.
    :param terminal_id: Terminal resource id for the probe path.
    :param bridge_dir: Native Claude bridge directory. When provided,
        each reconnect reads the active session id so attaches follow
        ``/clear`` terminal transfers.
    :param active_session_id_reader: Optional callback that returns
        the latest active Omnigent session id, e.g. ``"conv_new"``. This is
        used by other terminal-first wrappers that share the reconnect
        loop but store active session state outside Claude's bridge.
    :param close_attach_on_terminal_gone: When ``True``, pass a
        client-side terminal-gone watcher into
        :func:`attach_local_terminal`. The watcher closes the local
        WebSocket as soon as the terminal resource reports stopped, so
        CLI exit does not wait for delayed server-side close
        propagation.
    :returns: :attr:`_AttachOutcome.DETACHED` when the user detached
        from tmux (the runner should be kept alive); otherwise
        :attr:`_AttachOutcome.EXITED`.
    """
    delay = _ATTACH_INITIAL_RECONNECT_DELAY_S
    first_attempt = True
    while True:
        current_session_id = session_id
        if active_session_id_reader is not None:
            current_session_id = active_session_id_reader() or current_session_id
        elif bridge_dir is not None:
            current_session_id = read_active_session_id(bridge_dir) or current_session_id
        current_attach_url = attach_url
        if base_url is not None and current_session_id is not None and terminal_id is not None:
            current_attach_url = _attach_url(base_url, current_session_id, terminal_id)
        if not first_attempt and recover is not None:
            try:
                await recover()
            except Exception:  # noqa: BLE001 — best-effort recovery
                _logger.warning(
                    "claude-native reconnect recovery callback raised; retrying attach anyway",
                    exc_info=True,
                )
        first_attempt = False
        try:
            attach_kwargs: dict[str, Any] = {"headers": headers}
            if (
                close_attach_on_terminal_gone
                and base_url is not None
                and current_session_id is not None
                and terminal_id is not None
            ):

                async def _terminal_gone_probe(
                    *,
                    probe_session_id: str = current_session_id,
                ) -> bool:
                    """
                    Check whether the terminal resource is gone.

                    :param probe_session_id: Session id captured for
                        this attach attempt, e.g. ``"conv_abc123"``.
                    :returns: ``True`` when the Omnigent terminal resource
                        is definitively stopped.
                    """
                    return await _is_terminal_resource_gone(
                        base_url=base_url,
                        headers=headers,
                        session_id=probe_session_id,
                        terminal_id=terminal_id,
                        timeout_s=_CLAUDE_TERMINAL_GONE_WATCH_HTTP_TIMEOUT_S,
                    )

                attach_kwargs["terminal_gone_probe"] = _terminal_gone_probe
            user_requested_exit = await attach(current_attach_url, **attach_kwargs)
        except ConnectionClosed as exc:
            if _is_terminal_detached_close(exc):
                # The user detached from tmux: the session (and Claude)
                # is still alive. Do NOT reconnect or tear anything
                # down — the caller keeps the runner serving the web UI.
                _logger.info("claude-native terminal detached (close 4405); leaving session live")
                return _AttachOutcome.DETACHED
            if _is_terminal_not_found_close(exc):
                latest_session_id = None
                if active_session_id_reader is not None:
                    latest_session_id = active_session_id_reader()
                elif bridge_dir is not None:
                    latest_session_id = read_active_session_id(bridge_dir)
                if latest_session_id and latest_session_id != current_session_id:
                    continue
                _logger.info("claude-native terminal is gone (close 4404); ending session")
                return _AttachOutcome.EXITED
            if recover is None:
                raise
            click.echo(
                f"\nClaude session connection lost ({exc}); reconnecting...",
                err=True,
            )
        except (WebSocketException, OSError, ConnectionError) as exc:
            if recover is None:
                raise
            click.echo(
                f"\nClaude session connection lost ({type(exc).__name__}: {exc}); reconnecting...",
                err=True,
            )
        else:
            if user_requested_exit or recover is None:
                return _AttachOutcome.EXITED
            if base_url is not None and session_id is not None and terminal_id is not None:
                terminal_gone = await _is_terminal_resource_gone(
                    base_url=base_url,
                    headers=headers,
                    session_id=current_session_id,
                    terminal_id=terminal_id,
                )
                if terminal_gone:
                    _logger.info(
                        "claude-native terminal resource is gone after clean close; ending session"
                    )
                    return _AttachOutcome.EXITED
            click.echo(
                "\nClaude session connection closed by server; reconnecting...",
                err=True,
            )
        await _sleep(delay)
        delay = min(delay * 2, _ATTACH_MAX_RECONNECT_DELAY_S)

async def _is_terminal_resource_gone(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
    timeout_s: float = 10.0,
) -> bool:
    """
    Probe the AP-side terminal resource to detect normal exit.

    Called by :func:`_attach_with_reconnect` after a *clean* server-side
    close (the WS ended without raising). That state is ambiguous: it
    can mean a server bounce mid-session (the wrapper should retry) or
    a clean tmux exit because Claude quit (the wrapper should stop).
    The runner's terminal-attach route emits close code ``4404`` when
    the resource is already marked stopped before attach, but a
    teardown that races attach can produce a code-``1000`` close from
    the PTY bridge instead. This GET disambiguates the two states.

    HTTP / connection errors are treated as "not gone" so a server
    that's still bouncing (probe also fails) keeps the wrapper in the
    retry loop instead of exiting prematurely. The 4404 close code
    remains the authoritative kill signal handled in
    :func:`_attach_with_reconnect`.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for the Omnigent server. Mutated in
        place by the recover callback in remote mode; passing the
        same dict reference picks up the current bearer.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :param timeout_s: HTTP timeout in seconds for the probe,
        e.g. ``1.0`` for the attach-time watcher.
    :returns: ``True`` when the resource is definitively gone (404 or
        ``metadata.running is False``). ``False`` for any other
        response, including transport errors, so the loop keeps
        retrying.
    """
    path = (
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_s),
        ) as client:
            resp = await client.get(path)
    except (httpx.HTTPError, OSError):
        # Server is likely still bouncing; let the loop retry the
        # attach. The eventual 4404 close (or a subsequent successful
        # attach) decides the outcome.
        return False
    if resp.status_code == 404:
        return True
    if resp.status_code != 200:
        return False
    try:
        payload = resp.json()
    except ValueError:
        return False
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return metadata.get("running") is False

async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for :func:`asyncio.sleep` in the reconnect
    loop — see ``omnigent-testing`` skill rule 14 for why globally
    patching ``asyncio.sleep`` is banned.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)

def _is_terminal_not_found_close(exc: ConnectionClosed) -> bool:
    """
    Return whether *exc* indicates the terminal resource is gone.

    The runner closes the attach WebSocket with code
    ``WS_CLOSE_TERMINAL_NOT_FOUND`` (``4404``) when there is no
    matching terminal in its resource registry — typically because
    Claude exited and the tmux session terminated. Reconnecting in
    that state would just hit the same close, so the reconnect loop
    treats this code as a terminal exit signal.

    :param exc: WebSocket close exception raised during attach.
    :returns: ``True`` when the close code matches ``4404``;
        ``False`` otherwise (including when the close code is
        unavailable, e.g. for a TCP-level disconnect).
    """
    rcvd = exc.rcvd
    if rcvd is None:
        return False
    return rcvd.code == WS_CLOSE_TERMINAL_NOT_FOUND

def _is_terminal_detached_close(exc: ConnectionClosed) -> bool:
    """
    Return whether *exc* indicates the user detached from tmux.

    The runner's PTY bridge closes the attach WebSocket with code
    ``WS_CLOSE_TERMINAL_DETACHED`` (``4405``) when the ``tmux attach``
    child exits but ``has-session`` confirms the session is still
    alive — i.e. the user pressed the tmux detach key. Unlike a 4404
    (terminal gone), this must NOT end the session: the runner keeps
    running so the web UI stays connected.

    :param exc: WebSocket close exception raised during attach.
    :returns: ``True`` when the close code matches ``4405``;
        ``False`` otherwise (including when the close code is
        unavailable, e.g. for a TCP-level disconnect).
    """
    rcvd = exc.rcvd
    if rcvd is None:
        return False
    return rcvd.code == WS_CLOSE_TERMINAL_DETACHED

async def _close_claude_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Best-effort close of the AP-side Claude terminal resource on exit.

    Issued after the local attach loop returns so subsequent web
    attaches see the resource as stopped rather than waiting on
    runner-disconnect signaling. Failures are intentionally silenced —
    the local wrapper is already exiting and a stop notification is
    not load-bearing.
    """
    path = (
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=httpx.Timeout(10.0)
        ) as client:
            await client.delete(path)

async def _wait_for_claude_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> str:
    """
    Poll until the runner has auto-created the Claude terminal.

    A daemon-spawned runner brings the terminal up itself
    (``_auto_create_claude_terminal``) once it is notified of the
    session, so the CLI waits for the resource to appear rather than
    creating it.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``60.0``.
    :returns: The terminal resource id, e.g. ``"terminal_claude_main"``.
    :raises click.ClickException: If no terminal appears in time.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        terminal_id = await _find_running_claude_terminal(client, session_id)
        if terminal_id is not None:
            return terminal_id
        await asyncio.sleep(DAEMON_POLL_INTERVAL_S)
    raise click.ClickException(
        f"The runner did not create the Claude terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )

async def _ensure_claude_terminal_on_runner(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """
    Ask the bound runner to ensure the session's Claude terminal exists.

    Used on the resume path: when the CLI reattaches to a session whose
    daemon runner is still online but whose terminal was torn down (the
    auto-create only fires on session-start, not on runner reuse), this
    POSTs an "ensure" request — no ``spec`` and no ``bridge_inject_dir``,
    which the runner routes to ``_auto_create_claude_terminal`` (the full
    native setup, incl. cold resume) rather than a generic launch. The
    runner makes it idempotent: it returns the live terminal if one is
    already running, so this is a cheap no-op for the common
    runner-and-terminal-still-alive resume.

    Best-effort: a failure here is not fatal — the subsequent
    :func:`_wait_for_claude_terminal_ready` poll surfaces the clear error
    if the terminal still never appears.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    with contextlib.suppress(httpx.HTTPError):
        await client.post(
            f"/v1/sessions/{url_component(session_id)}/resources/terminals",
            # ``ensure_native_terminal`` is the explicit signal that routes this
            # to the full claude-native auto-create (incl. cold resume) on the
            # runner. A bare ``{terminal, session_key}`` body is ambiguous with
            # a plain generic launch, so the runner keys on this marker — not on
            # the absence of ``spec``/``bridge_inject_dir``.
            json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            timeout=60.0,
        )

async def _prepare_claude_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    claude_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_profiler: StartupProfiler | None = None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedClaudeTerminal:
    """
    Create/resolve a session and bring its terminal up via the daemon.

    Unlike :func:`_prepare_claude_terminal` (which binds a CLI-spawned
    runner and POSTs the terminal itself), this persists the launch args
    on the session and lets the daemon-spawned runner bring the terminal
    up — applying those args, the persisted model, cold resume, and the
    ucode gateway auth, all runner-side. The session is created *without*
    a bridge-id label so the bridge dir keys by session id, matching the
    runner's auto-create convention. See
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for Omnigent requests.
    :param session_id: Existing session id to resume, or ``None`` to
        create a fresh session from *session_bundle*.
    :param session_bundle: Gzipped agent bundle, required when
        *session_id* is ``None``.
    :param claude_args: User pass-through ``claude`` args. ``--resume``
        is stripped (the runner derives it from the session's
        ``external_session_id``); the rest are persisted as the
        session's ``terminal_launch_args`` so the runner launches with
        them. On resume, non-empty args replace the stored set
        (last-write-wins); empty reuses the stored set.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param workspace: Absolute host path for the runner cwd, e.g.
        ``"/Users/me/proj"``.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details (with tmux coordinates when the
        runner is local, enabling the direct-attach fast path).
    :raises click.ClickException: If any setup step fails.
    """
    from omnigent.claude_native_bridge import bridge_dir_for_conversation_id

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    persist_args = list(_strip_resume_from_claude_args(claude_args))
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        startup_profiler.mark("daemon prepare http client ready")
        # Resuming an existing session must not re-close its terminal on
        # exit; a fresh launch owns teardown.
        reattached = session_id is not None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Claude session requires a session bundle.")
            _mark_startup_step(
                startup_profiler,
                "creating daemon claude session",
                startup_progress=startup_progress,
                progress_message="Creating Claude session...",
            )
            session_id = await _create_claude_session(
                client,
                session_bundle,
                bridge_id=None,
                terminal_launch_args=persist_args or None,
            )
            _mark_startup_step(
                startup_profiler,
                "daemon claude session created",
                startup_progress=startup_progress,
            )
        elif persist_args:
            # Resume with new flags: replace the stored args
            # (last-write-wins). No new flags → leave the stored set so
            # the runner reuses them.
            _mark_startup_step(
                startup_profiler,
                "persisting resume launch args",
                startup_progress=startup_progress,
                progress_message="Updating Claude session...",
            )
            await client.patch(
                f"/v1/sessions/{url_component(session_id)}",
                json={"terminal_launch_args": persist_args},
            )
            _mark_startup_step(
                startup_profiler,
                "resume launch args persisted",
                startup_progress=startup_progress,
            )
        _mark_startup_step(
            startup_profiler,
            "waiting for host online",
            startup_progress=startup_progress,
        )
        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _mark_startup_step(
            startup_profiler,
            "host online",
            startup_progress=startup_progress,
        )
        _mark_startup_step(
            startup_profiler,
            "launching or reusing daemon runner",
            startup_progress=startup_progress,
            progress_message="Starting runner...",
        )
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _mark_startup_step(
            startup_profiler,
            "daemon runner launch requested",
            startup_progress=startup_progress,
            detail=f"runner={runner_id}",
        )
        _mark_startup_step(
            startup_profiler,
            "waiting for runner online",
            startup_progress=startup_progress,
            progress_message="Waiting for runner...",
        )
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        _mark_startup_step(
            startup_profiler,
            "daemon runner online",
            startup_progress=startup_progress,
        )
        if reattached:
            # Resume onto an already-online daemon runner reuses it without
            # re-running the session-start auto-create, so a runner whose
            # terminal was torn down (e.g. after a ``-p`` one-shot) comes
            # back terminal-less and the wait below would time out. Ask the
            # runner to ensure the claude terminal: idempotent (returns the
            # live one if present) and otherwise auto-creates it with cold
            # resume so history is restored. A fresh launch already creates
            # it on session-start, so this is only needed when reattaching.
            _mark_startup_step(
                startup_profiler,
                "ensuring resumed terminal on runner",
                startup_progress=startup_progress,
                progress_message="Restoring Claude terminal...",
            )
            await _ensure_claude_terminal_on_runner(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "resumed terminal ensure requested",
                startup_progress=startup_progress,
            )
        _mark_startup_step(
            startup_profiler,
            "waiting for claude terminal ready",
            startup_progress=startup_progress,
            progress_message="Starting Claude terminal...",
        )
        terminal_id = await _wait_for_claude_terminal_ready(
            client, session_id, timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S
        )
        _mark_startup_step(
            startup_profiler,
            "claude terminal ready",
            startup_progress=startup_progress,
            progress_message="Claude terminal ready.",
        )
        tmux = await _read_claude_terminal_tmux(client, session_id)
        _mark_startup_step(
            startup_profiler,
            "daemon terminal tmux metadata read",
            startup_progress=startup_progress,
        )
    return PreparedClaudeTerminal(
        session_id=session_id,
        terminal_id=terminal_id,
        bridge_dir=bridge_dir_for_conversation_id(session_id),
        reattached=reattached,
        tmux_socket=tmux.socket,
        tmux_target=tmux.target,
    )

async def _prepare_claude_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    runner_id: str | None,
    session_bundle: bytes | None,
    claude_args: tuple[str, ...],
    command: str,
    claude_config: ClaudeNativeUcodeConfig | None = None,
    startup_profiler: StartupProfiler | None = None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedClaudeTerminal:
    """
    Create/bind a session and launch its Claude terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for the Omnigent server.
    :param session_id: Optional existing session id.
    :param runner_id: Runner id to bind to the session.
    :param session_bundle: Gzipped agent bundle for new sessions.
        Required when *session_id* is ``None``.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param claude_config: Optional ucode-derived Claude Code config.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details.
    :raises click.ClickException: If any server operation fails.
    """
    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        startup_profiler.mark("prepare http client ready")
        cold_resume_args: tuple[str, ...] = ()
        # Cold resume = session existed but no live terminal. Even when
        # ``_resolve_cold_resume_args`` returns ``()`` (no captured
        # external_session_id, so Claude starts a fresh transcript),
        # Omnigent already holds the prior conversation history from the
        # earlier run. The forwarder must not re-read whatever the new
        # transcript file contains at startup and republish it as new
        # Omnigent events. Both subcases — injected ``--resume <claude_sid>``
        # and the warn-and-fallback path — share this hazard, so a
        # single ``cold_resumed`` flag covers both.
        cold_resumed = False
        bridge_id: str | None = None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Claude session requires a session bundle.")
            _mark_startup_step(
                startup_profiler,
                "creating claude session",
                startup_progress=startup_progress,
                progress_message="Creating Claude session...",
            )
            bridge_id = secrets.token_urlsafe(24)
            session_id = await _create_claude_session(
                client,
                session_bundle,
                bridge_id=bridge_id,
            )
            _mark_startup_step(
                startup_profiler,
                "claude session created",
                startup_progress=startup_progress,
            )
        else:
            _mark_startup_step(
                startup_profiler,
                "fetching resume session labels",
                startup_progress=startup_progress,
                progress_message="Loading Claude session...",
            )
            labels = await _fetch_claude_session_labels(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "resume session labels fetched",
                startup_progress=startup_progress,
            )
            bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
            _mark_startup_step(
                startup_profiler,
                "checking existing terminal",
                startup_progress=startup_progress,
            )
            existing_terminal_id = await _find_running_claude_terminal(client, session_id)
            if existing_terminal_id is not None:
                _mark_startup_step(
                    startup_profiler,
                    "existing terminal found",
                    startup_progress=startup_progress,
                )
                reattach_tmux = await _read_claude_terminal_tmux(client, session_id)
                _mark_startup_step(
                    startup_profiler,
                    "existing terminal tmux metadata read",
                    startup_progress=startup_progress,
                )
                return PreparedClaudeTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal_id,
                    bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                    reattached=True,
                    tmux_socket=reattach_tmux.socket,
                    tmux_target=reattach_tmux.target,
                )
            # Session exists but no live terminal — recover claude's prior transcript via --resume.
            _mark_startup_step(
                startup_profiler,
                "resolving cold resume args",
                startup_progress=startup_progress,
                progress_message="Restoring Claude session...",
            )
            cold_resume_args = await _resolve_cold_resume_args(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "cold resume args resolved",
                startup_progress=startup_progress,
            )
            cold_resumed = True

        if runner_id is not None:
            _mark_startup_step(
                startup_profiler,
                "binding session runner",
                startup_progress=startup_progress,
            )
            await _bind_session_runner(client, session_id, runner_id)
            _mark_startup_step(
                startup_profiler,
                "session runner bound",
                startup_progress=startup_progress,
            )
        bridge_dir = prepare_bridge_dir(
            session_id,
            bridge_id=bridge_id,
            workspace=Path.cwd(),
            launch_model=claude_config.model if claude_config else None,
        )
        _mark_startup_step(
            startup_profiler,
            "bridge dir prepared",
            startup_progress=startup_progress,
        )
        reset_transcript_forward_state(bridge_dir)
        _mark_startup_step(
            startup_profiler,
            "transcript forward state reset",
            startup_progress=startup_progress,
        )
        # Cold-resume args first so user-supplied tail args keep their relative position.
        _mark_startup_step(
            startup_profiler,
            "launching claude terminal",
            startup_progress=startup_progress,
            progress_message="Starting Claude terminal...",
        )
        terminal_id = await _launch_claude_terminal(
            client,
            session_id,
            (*cold_resume_args, *claude_args),
            command=command,
            bridge_dir=bridge_dir,
            claude_config=claude_config,
        )
        _mark_startup_step(
            startup_profiler,
            "claude terminal launched",
            startup_progress=startup_progress,
            progress_message="Claude terminal ready.",
        )
        # Read the runner's tmux coordinates while the client is open so
        # the attach step can prefer a direct local tmux attach.
        launch_tmux = await _read_claude_terminal_tmux(client, session_id)
        _mark_startup_step(
            startup_profiler,
            "terminal tmux metadata read",
            startup_progress=startup_progress,
        )
    return PreparedClaudeTerminal(
        session_id=session_id,
        terminal_id=terminal_id,
        bridge_dir=bridge_dir,
        reattached=False,
        cold_resumed=cold_resumed,
        tmux_socket=launch_tmux.socket,
        tmux_target=launch_tmux.target,
    )

async def _create_claude_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    bridge_id: str | None,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """
    Create a bundled terminal-first Claude session.

    Leaves ``title`` unset so the server's generic seed helper
    populates it from the first forwarded user message — the same
    path every other session type takes. The sidebar renders a
    ``"Claude Code"`` default label off the
    ``omnigent.wrapper = claude-code-native-ui`` label until the
    real title lands, so no server-side placeholder is needed.

    :param client: HTTP client pointed at the Omnigent server.
    :param bundle: Gzipped Claude wrapper agent bundle.
    :param bridge_id: Opaque bridge id to write on the session labels,
        e.g. ``"bridge_abc123"``. ``None`` omits the label so every
        consumer keys the bridge dir by the session id instead — the
        convention the runner's own auto-create path uses, so a
        daemon-routed launch (where the runner brings the terminal up)
        stays consistent. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param terminal_launch_args: Pass-through ``claude`` CLI args to
        persist on the session, e.g.
        ``["--dangerously-skip-permissions"]``. The runner reads these
        and applies them when it auto-launches the terminal. ``None``
        (the CLI-direct path, which passes args via the live terminal
        POST instead) persists nothing.
    :returns: New session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If creation fails.
    """
    labels = dict(_SESSION_LABELS)
    if bridge_id is not None:
        labels[BRIDGE_ID_LABEL_KEY] = bridge_id
    metadata: dict[str, Any] = {"labels": labels}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    # Stamp the wrapped claude's real effortLevel so the pill isn't a guess.
    effort = read_user_effort_level()
    if effort is not None:
        metadata["reasoning_effort"] = effort
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("claude-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise click.ClickException("Claude session creation response did not include session_id.")
    return session_id

async def _launch_claude_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    claude_args: tuple[str, ...],
    *,
    command: str,
    bridge_dir: Path,
    claude_config: ClaudeNativeUcodeConfig | None = None,
) -> str:
    """
    Launch the server-backed Claude terminal resource.

    :param client: HTTP client pointed at the Omnigent server. Its
        ``base_url`` and ``headers`` are reused as the
        ``PermissionRequest`` command hook's Omnigent URL and auth. The hook
        subprocess posts back to the same server with the same auth the
        wrapper already negotiated.
    :param session_id: Session/conversation id.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param bridge_dir: Bridge directory shared with Claude's MCP
        MCP server and the web-chat harness.
    :param claude_config: Optional ucode-derived Claude Code config.
    :returns: Terminal resource id.
    :raises click.ClickException: If terminal launch fails.
    """
    body = _claude_terminal_request(
        claude_args,
        command=command,
        bridge_dir=bridge_dir,
        ap_server_url=str(client.base_url),
        ap_auth_headers=dict(client.headers),
        claude_config=claude_config,
    )
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude terminal launch failed ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Claude terminal launch response did not include terminal id.")
    return terminal_id

async def _find_running_claude_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> str | None:
    """
    Return the existing running ``claude/main`` terminal id if present.

    Lookup happens before rebinding an existing session to this
    invocation's local runner. That preserves reattach behavior for a
    live terminal hosted by the currently bound runner; if the session
    has no runner, the runner is offline, or the terminal is absent,
    callers deterministically bind the current local runner and launch
    a fresh terminal.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: The deterministic Claude terminal id, or ``None`` when
        the wrapper should launch a new terminal.
    :raises click.ClickException: If the server rejects the lookup for
        a reason other than "not currently attachable".
    """
    terminal_id = claude_terminal_resource_id()
    resp = await client.get(
        (
            f"/v1/sessions/{url_component(session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}"
        ),
        timeout=30.0,
    )
    if resp.status_code == 200:
        payload = resp.json()
        if payload.get("id") != terminal_id or payload.get("type") != "terminal":
            raise click.ClickException(
                "Claude terminal lookup returned an unexpected resource shape."
            )
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("running") is False:
            return None
        return terminal_id
    if resp.status_code in {404, 409, 502, 503}:
        return None
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude terminal lookup failed ({resp.status_code}): {error_text(resp)}"
        )
    return None

async def _read_claude_terminal_tmux(
    client: httpx.AsyncClient,
    session_id: str,
) -> _ClaudeTerminalTmux:
    """
    Read the tmux socket/target the Claude terminal resource exposes.

    Lets the caller decide whether to attach to the runner's tmux
    directly (same machine, low latency) instead of relaying over the
    WebSocket PTY bridge. Best-effort: any lookup failure, non-200, or
    missing metadata yields ``(None, None)``, which callers treat as
    "not locally attachable" and fall back to the WebSocket path.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: The tmux coordinates, or ``_ClaudeTerminalTmux(None,
        None)`` when unavailable.
    """
    terminal_id = claude_terminal_resource_id()
    try:
        resp = await client.get(
            f"/v1/sessions/{url_component(session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}",
            timeout=30.0,
        )
    except httpx.HTTPError:
        return _ClaudeTerminalTmux(socket=None, target=None)
    if resp.status_code != 200:
        return _ClaudeTerminalTmux(socket=None, target=None)
    metadata = resp.json().get("metadata")
    if not isinstance(metadata, dict):
        return _ClaudeTerminalTmux(socket=None, target=None)
    raw_socket = metadata.get("tmux_socket")
    raw_target = metadata.get("tmux_target")
    socket = Path(raw_socket) if isinstance(raw_socket, str) and raw_socket else None
    target = raw_target if isinstance(raw_target, str) and raw_target else None
    return _ClaudeTerminalTmux(socket=socket, target=target)

def _claude_terminal_request(
    claude_args: tuple[str, ...],
    *,
    command: str,
    bridge_dir: Path,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    claude_config: ClaudeNativeUcodeConfig | None = None,
) -> dict[str, Any]:
    """
    Build the terminal resource creation body for Claude Code.

    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param bridge_dir: Bridge directory shared with Claude's MCP
        server and the web-chat harness.
    :param ap_server_url: Omnigent server base URL passed through to
        :func:`augment_claude_args` so Claude's
        ``PermissionRequest`` command hook is registered against the
        live Omnigent server.
    :param ap_auth_headers: Auth headers for the
        ``PermissionRequest`` command hook.
    :param claude_config: Optional ucode-derived Claude Code config.
    :returns: JSON body for ``POST /resources/terminals``.
    """
    claude_args = _merge_default_model_arg(
        claude_args,
        model=claude_config.model if claude_config is not None else None,
    )
    args = augment_claude_args(
        claude_args,
        bridge_dir=bridge_dir,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        api_key_helper=claude_config.api_key_helper if claude_config is not None else None,
    )
    spec: dict[str, Any] = {
        "command": command,
        "args": args,
        "os_env_type": "caller_process",
        # Pin the terminal cwd to the user's launch directory.
        # The wrapper runs locally on the same host as the runner
        # subprocess, so ``Path.cwd()`` here equals the runner's
        # ``RUNNER_WORKSPACE`` env. Without this, the runner falls
        # through to ``SessionResourceRegistry.compute_default_env_root``
        # which (under ``per_session_workspace=True``, set
        # whenever ``runner_workspace`` is non-None) returns
        # ``<workspace>/<conversation_id>`` -- a path the runner
        # never actually creates. tmux is then launched with
        # ``-c <that-missing-dir>`` and silently falls back to
        # ``$HOME``, so ``claude`` starts in the wrong directory.
        # The per-session isolation is meaningful for shared
        # deployments; ``omnigent claude`` is a local-only
        # single-user wrapper, so taking the explicit-cwd path
        # short-circuits it safely.
        "cwd": str(Path.cwd().resolve()),
        "scrollback": _CLAUDE_TERMINAL_SCROLLBACK_LINES,
    }
    spec["env"] = build_native_claude_terminal_env(claude_config)
    if claude_config is not None:
        # The runner's terminal layer inherits the parent process env.
        # Remove provider/session variables that can override the
        # ucode apiKeyHelper or make Claude think it is nested.
        unset_env_vars = [
            _ANTHROPIC_API_KEY_ENV,
            _CLAUDE_CODE_NESTED_SESSION_ENV,
        ]
        env_args = [part for var in unset_env_vars for part in ("-u", var)]
        spec["command"] = "env"
        spec["args"] = [*env_args, command, *args]
    return {
        "terminal": _TERMINAL_NAME,
        "session_key": _TERMINAL_SESSION_KEY,
        "spec": spec,
        # Boolean opt-in; the runner derives the bridge dir from session_id.
        "bridge_inject_dir": True,
    }

def _merge_default_model_arg(
    claude_args: tuple[str, ...],
    *,
    model: str | None,
) -> tuple[str, ...]:
    """
    Add a ucode model default unless the user already selected one.

    :param claude_args: User-provided Claude Code args, e.g.
        ``("--model", "sonnet")``.
    :param model: Ucode model id, e.g.
        ``"databricks-claude-opus-4-7"``.
    :returns: Args with ``--model <model>`` appended when appropriate.
    """
    if not model:
        return claude_args
    for arg in claude_args:
        if arg == "--model" or arg.startswith("--model="):
            return claude_args
    return (*claude_args, "--model", model)

async def attach_local_terminal(
    attach_url: str,
    *,
    headers: dict[str, str],
    stdin_fd: int | None = None,
    stdout_fd: int | None = None,
    terminal_gone_probe: Callable[[], Awaitable[bool]] | None = None,
    terminal_gone_watch_interval_s: float = _CLAUDE_TERMINAL_GONE_WATCH_INTERVAL_S,
) -> bool:
    """
    Attach the local TTY to an Omnigent terminal WebSocket.

    :param attach_url: Fully-qualified ``ws://`` or ``wss://`` attach
        URL.
    :param headers: WebSocket handshake headers, e.g.
        ``{"Authorization": "Bearer ..."}``.
    :param stdin_fd: File descriptor to read local input from.
        ``None`` uses ``sys.stdin``.
    :param stdout_fd: File descriptor to write terminal output to.
        ``None`` uses ``sys.stdout``.
    :param terminal_gone_probe: Optional async callback returning
        ``True`` once the Omnigent terminal resource is stopped. When set,
        the client closes its WebSocket locally instead of waiting for
        the server close frame to propagate.
    :param terminal_gone_watch_interval_s: Poll interval for
        ``terminal_gone_probe`` in seconds, e.g. ``0.25``.
    :returns: ``True`` when the local user requested termination
        (stdin EOF). ``False`` when the WebSocket closed for any
        other reason (server bounce, runner restart, clean close
        initiated by the server). On SIGTERM/SIGHUP, ``SystemExit``
        propagates before this function returns. Callers use the
        boolean to decide whether to reconnect or exit cleanly.
    """
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd

    stdin_eof = asyncio.Event()
    async with _websocket_connect(attach_url, headers=headers) as ws:
        old_attrs = _enter_raw_mode(stdin_fd)
        signal_restore = _install_attach_signal_handlers(ws, stdin_fd)
        try:
            await _send_resize(ws, stdin_fd)
            stop_waiter = signal_restore.stop_event.wait()
            tasks = {
                asyncio.create_task(
                    _stdin_to_websocket(ws, stdin_fd, eof_event=stdin_eof),
                    name="claude-stdin-to-ws",
                ),
                asyncio.create_task(
                    _websocket_to_stdout(ws, stdout_fd), name="claude-ws-to-stdout"
                ),
                asyncio.create_task(stop_waiter, name="claude-attach-signal"),
            }
            if terminal_gone_probe is not None:
                tasks.add(
                    asyncio.create_task(
                        _close_ws_when_terminal_gone(
                            ws,
                            terminal_gone_probe=terminal_gone_probe,
                            poll_interval_s=terminal_gone_watch_interval_s,
                        ),
                        name="claude-terminal-gone-watcher",
                    )
                )
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            signal_restore.restore()
            _restore_terminal(stdin_fd, old_attrs)
        if signal_restore.received_signal is not None:
            raise SystemExit(128 + signal_restore.received_signal)
    return stdin_eof.is_set()

async def _close_ws_when_terminal_gone(
    ws: Any,
    *,
    terminal_gone_probe: Callable[[], Awaitable[bool]],
    poll_interval_s: float,
) -> None:
    """
    Close the client WebSocket when the Omnigent terminal resource stops.

    This is a client-side fast-exit path for native Claude shutdown:
    the runner can mark the terminal stopped before the attach
    WebSocket close frame reaches the CLI. Closing locally unblocks
    the stdout bridge without waiting for delayed close propagation.

    :param ws: Connected ``websockets`` client.
    :param terminal_gone_probe: Async callback returning ``True``
        when the terminal resource is stopped.
    :param poll_interval_s: Seconds to sleep between probes,
        e.g. ``0.25``.
    :returns: None after closing the WebSocket, or when cancelled.
    """
    while True:
        await asyncio.sleep(poll_interval_s)
        terminal_gone = await terminal_gone_probe()
        if not terminal_gone:
            continue

        try:
            await ws.close(code=1000, reason="terminal resource stopped")
        except (WebSocketException, OSError, ConnectionError):
            _logger.debug(
                "claude-native terminal-gone watcher close failed",
                exc_info=True,
            )
        return


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
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
    for _key, _value in _sib_remote_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_resume_ui.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
