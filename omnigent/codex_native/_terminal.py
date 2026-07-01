"""Native Codex TUI wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.claude_native import (
    _attach_with_reconnect,
    attach_local_terminal,
)
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    build_codex_native_server,
    build_codex_remote_args,
    client_for_transport,
    codex_session_meta_model_provider,
    codex_terminal_env,
    preload_codex_thread_for_resume,
    resolve_native_codex_launch,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_state,
    socket_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.codex_native_forwarder import supervise_forwarder
from omnigent.codex_native_state import read_launch_state, write_launch_state
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
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

async def _prepare_codex_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    codex_args: tuple[str, ...],
    model: str | None,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCodexTerminal:
    """
    Create or resume a Codex-native session through a daemon runner.

    The runner owns the Codex app-server, transcript forwarder, and tmux
    terminal. The CLI only persists launch intent, waits for the terminal
    resource, and attaches to it.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com"``.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Existing session id to resume, or ``None`` for a
        fresh session.
    :param session_bundle: Gzipped Codex wrapper bundle. Required when
        *session_id* is ``None``.
    :param codex_args: User pass-through Codex args, e.g.
        ``("--config", "approval_policy=on-request")``.
    :param model: Optional model override for this launch, e.g.
        ``"gpt-5.4-mini"``.
    :param host_id: Local host daemon id, e.g. ``"host_abc123"``.
    :param workspace: Absolute workspace path for the runner cwd, e.g.
        ``"/Users/me/repo"``.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details for attaching.
    :raises click.ClickException: If setup fails.
    """
    persist_args = list(codex_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = session_id is not None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Codex session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Codex session...")
            session_id = await _create_codex_session(
                client,
                session_bundle,
                bridge_id=None,
                terminal_launch_args=persist_args or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading Codex session...")
            payload = await _fetch_codex_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a codex-native session."
                )
            existing_terminal = await _find_running_codex_terminal(client, session_id)
            if existing_terminal is not None:
                external_session_id = payload.get("external_session_id")
                thread_id = external_session_id if isinstance(external_session_id, str) else None
                if persist_args or model is not None:
                    click.echo(
                        "Ignoring Codex launch args/model for an already-running "
                        "terminal; restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Codex terminal ready.")
                return PreparedCodexTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    bridge_dir=bridge_dir_for_bridge_id(session_id),
                    thread_id=thread_id,
                    app_server_url=None,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                )
            patch: dict[str, Any] = {}
            if persist_args:
                patch["terminal_launch_args"] = persist_args
            if model is not None:
                patch["model_override"] = model
            if patch:
                _update_startup_progress(startup_progress, "Updating Codex session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json=patch,
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Codex session launch config update failed "
                        f"({resp.status_code}): {error_text(resp)}"
                    )

        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _update_startup_progress(startup_progress, "Starting runner...")
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _update_startup_progress(startup_progress, "Waiting for runner...")
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        # Must run AFTER wait_for_runner_online — unregistered runners
        # 400 on replace_runner_id. The daemon bind paths don't route
        # through replace_runner_id, so without this re-bind a stopped
        # session stays stopped.
        await _bind_session_runner(client, session_id, runner_id)
        _update_startup_progress(startup_progress, "Starting Codex terminal...")
        await _ensure_codex_terminal_on_runner(client, session_id)
        terminal = await _wait_for_codex_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Codex terminal ready.")
    return PreparedCodexTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        bridge_dir=bridge_dir_for_bridge_id(session_id),
        thread_id=None,
        app_server_url=None,
        app_server=None,
        event_client=None,
        reattached=reattached,
    )

async def _ensure_codex_terminal_on_runner(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """
    Ask the bound runner to ensure the Codex app-server and terminal exist.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: None.
    :raises click.ClickException: If the runner rejects the ensure request.
    """
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )

async def _wait_for_codex_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedCodexTerminal:
    """
    Wait until the runner exposes the Codex terminal resource.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``60.0``.
    :returns: Terminal details including direct tmux attach metadata when
        available.
    :raises click.ClickException: If no terminal appears in time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_codex_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Codex terminal for {session_id!r} within {timeout_s:.0f}s."
    )

async def _post_initial_prompt(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    prompt: str,
    auth: httpx.Auth | None,
) -> None:
    """
    Send the first Codex prompt through Omnigent instead of the app-server.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param prompt: User prompt text.
    :param auth: Optional refresh-capable HTTP auth for long-lived
        Databricks-backed sessions.
    :returns: None.
    :raises click.ClickException: If Omnigent rejects the prompt.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(30.0),
    ) as client:
        resp = await client.post(
            f"/v1/sessions/{url_component(session_id)}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex initial prompt failed ({resp.status_code}): {error_text(resp)}"
        )

async def _prepare_codex_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    runner_id: str | None,
    session_bundle: bytes | None,
    codex_args: tuple[str, ...],
    command: str,
    model: str | None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCodexTerminal:
    """
    Create/bind a session, start app-server, and launch Codex TUI.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Optional existing session id.
    :param runner_id: Runner id to bind.
    :param session_bundle: Gzipped agent bundle for new sessions.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable.
    :param model: Optional model id.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        bridge_id: str
        thread_id: str | None = None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Codex session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Codex session...")
            bridge_id = secrets.token_urlsafe(24)
            session_id = await _create_codex_session(client, session_bundle, bridge_id=bridge_id)
        else:
            _update_startup_progress(startup_progress, "Loading Codex session...")
            payload = await _fetch_codex_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a codex-native session."
                )
            bridge_id = str(labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)
            existing_terminal = await _find_running_codex_terminal(client, session_id)
            external_session_id = payload.get("external_session_id")
            thread_id = external_session_id if isinstance(external_session_id, str) else None
            if existing_terminal is not None and thread_id is not None:
                reattach_bridge_dir = bridge_dir_for_bridge_id(bridge_id)
                reattach_unix_socket = socket_path_for_bridge_dir(reattach_bridge_dir)
                # The running terminal's real transport lives in its bridge
                # state (``ws://`` for terminals launched by current code).
                # Reattach starts no app-server/forwarder/initial-turn, so
                # app_server_url is unused here, but populate it accurately
                # from bridge state and fall back to the legacy unix path.
                reattach_state = read_bridge_state(reattach_bridge_dir)
                reattach_transport = (
                    reattach_state.socket_path
                    if reattach_state is not None
                    else str(reattach_unix_socket)
                )
                _update_startup_progress(startup_progress, "Codex terminal ready.")
                return PreparedCodexTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    bridge_dir=reattach_bridge_dir,
                    thread_id=thread_id,
                    app_server_url=reattach_transport,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                )
            if thread_id is None:
                raise click.ClickException(
                    f"Conversation {session_id!r} is missing its Codex thread id."
                )

        bridge_dir = prepare_bridge_dir(bridge_id)
        socket_path = socket_path_for_bridge_dir(bridge_dir)
        codex_home = codex_home_for_bridge_dir(bridge_dir)
        clear_bridge_state(bridge_dir)
        # Route across all offerings: a configured provider (configure
        # harness), the Databricks ucode profile, or Codex's own login —
        # so `omnigent codex` honors the provider selection like the
        # in-process codex harness. Resolved before any rollout synthesis
        # so session_meta can name the provider the launch routes through.
        _codex_launch = resolve_native_codex_launch(model=model)
        if thread_id is not None:
            await _ensure_local_codex_resume_rollout(
                client,
                session_id=session_id,
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=Path.cwd().resolve(),
                model_provider=codex_session_meta_model_provider(_codex_launch),
                codex_path=command,
            )
        # Listen on a loopback WebSocket, mirroring the host-spawned
        # runner (``runner/app.py`` ``_auto_create_codex_terminal``).
        # Codex CLI ``app-server`` only accepts ``stdio://``, ``ws://``,
        # or ``off`` — it dropped ``unix://`` — so a ``unix://`` listen
        # exits immediately and the terminal (and the web-UI Terminal
        # pill) never appears.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _probe:
            _probe.bind(("127.0.0.1", 0))
            codex_ws_url = f"ws://127.0.0.1:{_probe.getsockname()[1]}"
        app_server = build_codex_native_server(
            socket_path=socket_path,
            codex_home=codex_home,
            cwd=Path.cwd(),
            model=_codex_launch.model,
            profile=_codex_launch.profile,
            codex_path=command,
            extra_config_overrides=_codex_launch.config_overrides,
            bridge_dir=bridge_dir,
            ap_server_url=base_url,
            ap_auth_headers=headers,
        )
        app_server.listen_url = codex_ws_url
        event_client: CodexAppServerClient | None = None
        terminal_id: str | None = None
        launched_terminal: LaunchedCodexTerminal | None = None
        try:
            await app_server.start()
            if thread_id is None:
                event_client = client_for_transport(
                    codex_ws_url,
                    client_name="omnigent-codex-native",
                )
                await event_client.connect()
            else:
                await preload_codex_thread_for_resume(codex_ws_url, thread_id)
                write_bridge_state(
                    bridge_dir,
                    CodexNativeBridgeState(
                        session_id=session_id,
                        socket_path=codex_ws_url,
                        thread_id=thread_id,
                        codex_home=str(codex_home),
                    ),
                )
            if runner_id is not None:
                await _bind_session_runner(client, session_id, runner_id)
            _update_startup_progress(startup_progress, "Starting Codex terminal...")
            launched_terminal = await _launch_codex_terminal(
                client,
                session_id,
                codex_args=codex_args,
                command=command,
                thread_id=thread_id,
                remote_url=codex_ws_url,
                env=codex_terminal_env(app_server),
                # Give the --remote TUI the same provider overrides as
                # the app-server so it resolves the Omnigent provider
                # and skips the OpenAI-login onboarding screen.
                config_overrides=tuple(app_server.config_overrides),
            )
            terminal_id = launched_terminal.terminal_id
            _update_startup_progress(startup_progress, "Codex terminal ready.")
        except Exception:
            if terminal_id is not None:
                await _close_codex_terminal(
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    terminal_id=terminal_id,
                )
            if event_client is not None:
                await event_client.close()
            await app_server.close()
            raise
    if launched_terminal is None:
        raise click.ClickException("Codex terminal was not launched.")
    return PreparedCodexTerminal(
        session_id=session_id,
        terminal_id=launched_terminal.terminal_id,
        tmux_socket=launched_terminal.tmux_socket,
        tmux_target=launched_terminal.tmux_target,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        app_server_url=codex_ws_url,
        app_server=app_server,
        event_client=event_client,
        reattached=False,
    )

async def _attach_with_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    prompt: str | None,
    recover: Any | None = None,
    auth: httpx.Auth | None = None,
) -> None:
    """
    Attach to the Codex terminal while forwarding app-server events.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details.
    :param prompt: Optional first prompt to send.
    :param recover: Optional reconnect recovery callback.
    :param auth: Optional long-lived HTTP auth for remote sessions.
    :returns: None.
    """
    forwarder: asyncio.Task[None] | None = None
    try:
        if prepared.thread_id is None:
            attach_task = asyncio.create_task(
                _attach_terminal_resource(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                    recover=recover,
                ),
                name="codex-native-terminal-attach",
            )
            await asyncio.sleep(0)
            try:
                prepared.thread_id = await _initialize_fresh_terminal_thread(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                )
                if prepared.app_server is not None:
                    forwarder = _start_codex_forwarder(
                        base_url=base_url,
                        headers=headers,
                        prepared=prepared,
                        auth=auth,
                    )
                    if prompt:
                        await _start_initial_turn(
                            prepared.app_server_url,
                            prepared.thread_id,
                            prompt,
                        )
                await attach_task
            except Exception:
                if not attach_task.done():
                    attach_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await attach_task
                raise
        else:
            if prepared.app_server is not None:
                forwarder = _start_codex_forwarder(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                    auth=auth,
                )
                if prompt:
                    await _start_initial_turn(prepared.app_server_url, prepared.thread_id, prompt)
            await _attach_terminal_resource(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                recover=recover,
            )
    finally:
        if forwarder is not None:
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forwarder
        if not prepared.reattached:
            active_session_id = (
                _active_codex_session_id(prepared.bridge_dir) or prepared.session_id
            )
            await _close_codex_terminal(
                base_url=base_url,
                headers=headers,
                session_id=active_session_id,
                terminal_id=prepared.terminal_id,
            )
        if prepared.app_server is not None:
            await prepared.app_server.close()

def _start_codex_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    auth: httpx.Auth | None,
) -> asyncio.Task[None]:
    """
    Start the transcript forwarder for a prepared Codex terminal.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details with a known thread id.
    :param auth: Optional long-lived HTTP auth for remote sessions.
    :returns: Running forwarder task.
    :raises click.ClickException: If the Codex thread id is not known.
    """
    if prepared.thread_id is None:
        raise click.ClickException("Codex thread id was not initialized.")
    return asyncio.create_task(
        supervise_forwarder(
            base_url=base_url,
            headers=headers,
            session_id=prepared.session_id,
            bridge_dir=prepared.bridge_dir,
            app_server_url=prepared.app_server_url,
            thread_id=prepared.thread_id,
            client=prepared.event_client,
            auth=auth,
        ),
        name="codex-native-forwarder",
    )

async def _initialize_fresh_terminal_thread(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
) -> str:
    """
    Wait for an attached fresh Codex TUI to create its app-server thread.

    Codex terminals can be launched in a tmux pane that waits for the
    first client attach before starting Codex. This preserves web
    terminal sharing while letting Codex query the real attached
    terminal during startup.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details whose ``thread_id`` is
        still ``None``.
    :returns: The Codex app-server thread id, e.g. ``"thread_abc123"``.
    :raises click.ClickException: If no thread-start listener exists.
    """
    if prepared.event_client is None:
        raise click.ClickException("Codex event listener was not initialized.")
    thread_id = await _wait_for_thread_started(prepared.event_client)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0),
    ) as client:
        await _patch_external_session_id(client, prepared.session_id, thread_id)
    write_bridge_state(
        prepared.bridge_dir,
        CodexNativeBridgeState(
            session_id=prepared.session_id,
            socket_path=prepared.app_server_url,
            thread_id=thread_id,
            codex_home=str(codex_home_for_bridge_dir(prepared.bridge_dir)),
        ),
    )
    return thread_id

async def _attach_terminal_resource(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    recover: Any | None,
) -> None:
    """
    Attach the current terminal to the prepared Omnigent terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details.
    :param recover: Optional reconnect recovery callback.
    :returns: None after the attach exits.
    """
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is None:
        if prepared.tmux_socket is None or prepared.tmux_target is None:
            raise click.ClickException("Codex tmux attach metadata was incomplete.")
        await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)
        return
    if prepared.app_server_url is None:
        raise click.ClickException(
            f"Runner-owned Codex terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    await _attach_with_reconnect(
        attach=attach_local_terminal,
        attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
        headers=headers,
        recover=recover,
        base_url=base_url,
        session_id=prepared.session_id,
        terminal_id=prepared.terminal_id,
        active_session_id_reader=lambda: _active_codex_session_id(prepared.bridge_dir),
    )

def _active_codex_session_id(bridge_dir: Path) -> str | None:
    """
    Return the active Omnigent session id for a native Codex bridge.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None`` when
        bridge state has not been written yet.
    """
    state = read_bridge_state(bridge_dir)
    return state.session_id if state is not None else None

def _can_attach_direct_tmux(prepared: PreparedCodexTerminal) -> bool:
    """
    Return whether this process can attach to the runner tmux directly.

    :param prepared: Prepared terminal details.
    :returns: ``True`` when the runner exposed a local tmux socket, the
        socket exists on this host, and ``tmux`` is available on PATH.
    """
    return _direct_tmux_unavailable_reason(prepared) is None

def _direct_tmux_unavailable_reason(prepared: PreparedCodexTerminal) -> str | None:
    """
    Explain why this process cannot attach to the runner tmux directly.

    :param prepared: Prepared terminal details.
    :returns: ``None`` when direct tmux attach is available, otherwise a
        human-readable reason for the missing prerequisite.
    """
    if prepared.tmux_socket is None:
        return "the terminal resource did not include a tmux socket path."
    if prepared.tmux_target is None:
        return "the terminal resource did not include a tmux target."
    if not prepared.tmux_socket.exists():
        return f"tmux socket {prepared.tmux_socket} is not reachable from this CLI process."
    if shutil.which("tmux") is None:
        return "tmux is not available on PATH."
    return None

async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    This avoids the local WebSocket + PTY relay used for browser and
    non-local runner attaches. ``TMUX`` is removed from the child
    environment so users who run ``omnigent codex`` inside their own
    tmux session can still attach to Omnigent' private tmux server.

    :param socket_path: Runner tmux socket path.
    :param tmux_target: Tmux target to attach, e.g. ``"main"``.
    :returns: None after the attach process exits.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
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
    await process.wait()

async def _create_codex_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    bridge_id: str | None,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """
    Create a bundled terminal-first Codex session.

    :param client: HTTP client pointed at AP.
    :param bundle: Gzipped agent bundle.
    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
        ``None`` omits the label so the runner-owned bridge keys by
        session id.
    :param terminal_launch_args: Pass-through Codex CLI args to persist
        for runner-owned terminal launch, e.g.
        ``["--config", "approval_policy=on-request"]``.
    :returns: New Omnigent session id.
    """
    labels = dict(_SESSION_LABELS)
    if bridge_id is not None:
        labels[CODEX_NATIVE_BRIDGE_ID_LABEL_KEY] = bridge_id
    metadata = {
        "labels": labels,
    }
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Codex session creation response did not include session_id.")
    return new_session_id

def _mint_codex_thread_id() -> str:
    """
    Mint a fresh UUIDv7 thread id for a forked Codex clone.

    Codex thread ids are UUIDv7 (time-ordered), e.g.
    ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``. A fork writes the cloned
    rollout under a freshly minted id (rather than reusing the source's)
    so the clone gets its own Omnigent ``external_session_id`` — mirroring how
    claude-native assigns the clone a new transcript uuid. The stdlib has
    no UUIDv7 generator before Python 3.14, so we assemble one per
    RFC 9562 §5.7 (48-bit millisecond timestamp + version + variant +
    random) rather than add a dependency.

    :returns: A UUIDv7 string, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    """
    unix_ms = int(time.time() * 1000)
    value = bytearray(unix_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    value[6] = (value[6] & 0x0F) | 0x70  # version 7 in the high nibble
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant (0b10)
    return str(uuid.UUID(bytes=bytes(value)))

async def _patch_external_session_id(
    client: httpx.AsyncClient,
    session_id: str,
    thread_id: str,
) -> None:
    """
    Persist the native Codex thread id on the Omnigent session.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param thread_id: Codex thread id.
    :returns: None.
    """
    resp = await client.patch(
        f"/v1/sessions/{url_component(session_id)}",
        json={"external_session_id": thread_id},
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex thread bind failed ({resp.status_code}): {error_text(resp)}"
        )

async def _wait_for_thread_started(client: CodexAppServerClient) -> str:
    """
    Wait for the Codex TUI to create its remote app-server thread.

    Thin CLI-flavoured wrapper over the canonical
    :func:`omnigent.codex_native_forwarder.wait_for_thread_started`
    (shared with the host-spawned runner auto-create), translating its
    plain exceptions into ``click.ClickException`` for the CLI.

    :param client: Connected app-server client listening on the session
        Unix socket.
    :returns: Codex thread id, e.g. ``"019e70d7-1233-7b53-9c76-f1df1f6b1dba"``.
    :raises click.ClickException: If no ``thread/started`` event arrives
        before the startup timeout, or the stream ends first.
    """
    from omnigent.codex_native_forwarder import wait_for_thread_started

    try:
        return await wait_for_thread_started(client, timeout=_CODEX_THREAD_START_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise click.ClickException(
            "Codex TUI did not start a remote app-server thread before "
            f"the {_CODEX_THREAD_START_TIMEOUT_SECONDS:.0f}s timeout."
        ) from exc
    except RuntimeError as exc:
        raise click.ClickException(
            "Codex app-server event stream ended before thread startup."
        ) from exc

async def _start_initial_turn(app_server_url: str, thread_id: str, prompt: str) -> None:
    """
    Submit an initial prompt to a native Codex thread.

    :param app_server_url: App-server transport to connect over, e.g.
        ``"ws://127.0.0.1:9876"``.
    :param thread_id: Codex thread id.
    :param prompt: Prompt text.
    :returns: None.
    """
    client = client_for_transport(app_server_url, client_name="omnigent-codex-native")
    await client.connect()
    try:
        await client.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
        )
    finally:
        await client.close()

async def _launch_codex_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    codex_args: tuple[str, ...],
    command: str,
    thread_id: str | None,
    remote_url: str,
    env: dict[str, str],
    config_overrides: tuple[str, ...] = (),
) -> LaunchedCodexTerminal:
    """
    Launch the server-backed Codex terminal resource.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable.
    :param thread_id: Codex thread id to resume. ``None`` starts a
        fresh remote Codex TUI thread.
    :param remote_url: App-server transport the Codex TUI attaches to
        via ``--remote``, e.g. ``"ws://127.0.0.1:9876"``.
    :param env: Environment overrides for the terminal process.
    :param config_overrides: Codex ``-c`` provider/model overrides to
        apply to the ``--remote`` TUI so it resolves the same provider
        as the app-server (and skips the OpenAI-login onboarding
        screen). See :func:`build_codex_remote_args`. Empty for a plain
        Codex-login launch. E.g.
        ``('model_provider="omnigent_databricks"',)``.
    :returns: Launched terminal resource details.
    """
    terminal_args = build_codex_remote_args(
        codex_args=codex_args,
        thread_id=thread_id,
        remote_url=remote_url,
        config_overrides=config_overrides,
    )
    body = {
        "terminal": _TERMINAL_NAME,
        "session_key": _TERMINAL_SESSION_KEY,
        "spec": {
            "command": command,
            "args": terminal_args,
            "os_env_type": "caller_process",
            "cwd": str(Path.cwd()),
            "env": env,
            "scrollback": _CODEX_TERMINAL_SCROLLBACK_LINES,
            "tmux_allow_passthrough": True,
            "tmux_start_on_attach": True,
        },
    }
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex terminal launch failed ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    return _launched_codex_terminal_from_payload(payload)

def _launched_codex_terminal_from_payload(payload: object) -> LaunchedCodexTerminal:
    """
    Decode terminal launch metadata returned by the runner.

    :param payload: Decoded terminal resource JSON object, e.g.
        ``{"id": "terminal_codex_main", "metadata": {...}}``.
    :returns: Launched terminal details.
    :raises click.ClickException: If the response omits a valid
        terminal id.
    """
    if not isinstance(payload, dict):
        raise click.ClickException("Codex terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Codex terminal launch response did not include terminal id.")
    metadata = payload.get("metadata")
    tmux_socket: Path | None = None
    tmux_target: str | None = None
    if isinstance(metadata, dict):
        raw_socket = metadata.get("tmux_socket")
        raw_target = metadata.get("tmux_target")
        if isinstance(raw_socket, str) and raw_socket:
            tmux_socket = Path(raw_socket)
        if isinstance(raw_target, str) and raw_target:
            tmux_target = raw_target
    return LaunchedCodexTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )

async def _find_running_codex_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedCodexTerminal | None:
    """
    Return the existing running Codex terminal id if present.

    Lookup happens before rebinding an existing session to this
    invocation's local runner. If the previously bound runner is
    offline, the resource route returns an unavailable status; treat
    that as a reattach miss so the caller can bind the current runner
    and cold-resume the Codex thread.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Terminal details, or ``None`` when absent.
    :raises click.ClickException: If the server rejects the lookup for
        a reason other than "not currently attachable".
    """
    terminal_id = codex_terminal_resource_id()
    resp = await client.get(
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    if _codex_terminal_lookup_is_reattach_miss(resp):
        return None
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch Codex terminal ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_codex_terminal_from_payload(payload)

def _codex_terminal_lookup_is_reattach_miss(resp: httpx.Response) -> bool:
    """
    Return whether a terminal lookup means "launch a replacement".

    Only missing terminals and explicit runner-unavailable states are
    safe to treat as a reattach miss. Other 409 / 502 / 503 responses
    can indicate real server or infrastructure failures and should
    stay loud.

    :param resp: HTTP response from the terminal resource lookup.
    :returns: ``True`` when Codex should cold-resume into a new
        terminal; ``False`` when the response should be handled by the
        normal status path.
    """
    if resp.status_code == 404:
        return True
    error_code = _response_error_code(resp)
    if error_code == _RUNNER_UNAVAILABLE_ERROR_CODE:
        return True
    message = error_text(resp)
    if resp.status_code == 503 and _runner_offline_message(message):
        return True
    if error_code == _CONFLICT_ERROR_CODE and _UNBOUND_RUNNER_MESSAGE_FRAGMENT in message:
        return True
    if resp.status_code == 409 and _UNBOUND_RUNNER_MESSAGE_FRAGMENT in message:
        return True
    return False

def _response_error_code(resp: httpx.Response) -> str | None:
    """
    Extract a structured Omnigent error code from *resp* if present.

    :param resp: HTTP response from AP.
    :returns: ``error.code`` when the JSON body has one, otherwise
        ``None``.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None

def _runner_offline_message(message: str) -> bool:
    """
    Return whether *message* is the Omnigent stale-runner error shape.

    :param message: Error text extracted from AP, e.g.
        ``"runner 'runner_abc' is offline for conversation 'conv_123'"``.
    :returns: ``True`` when the message specifically names an offline
        runner for the conversation being resumed.
    """
    return message.startswith("runner ") and _RUNNER_OFFLINE_MESSAGE_FRAGMENT in message

async def _close_codex_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Best-effort close of the AP-side Codex terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Omnigent session id.
    :param terminal_id: Terminal resource id.
    :returns: None.
    """
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(10.0),
        ) as client:
            await client.delete(
                f"/v1/sessions/{url_component(session_id)}"
                f"/resources/terminals/{url_component(terminal_id)}"
            )

def _preflight_local_tools() -> None:
    """
    Verify local executables required by the native Codex wrapper.

    :returns: None.
    :raises click.ClickException: If required tools are missing.
    """
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Codex wrapper "
            "attaches to the runner-owned Codex tmux terminal."
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _rollout as _sib_rollout
    from . import _session_items as _sib_session_items
    from . import _types as _sib_types
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
    for _key, _value in _sib_rollout.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_session_items.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
