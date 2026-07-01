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

def _materialize_codex_agent_spec(
    tmpdir: Path,
    *,
    model: str | None,
) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent codex``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :param model: Optional model id, e.g. ``"gpt-5.4-mini"``.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "codex-native-ui.yaml"
    executor: dict[str, str] = {"harness": "codex-native"}
    if model is not None:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Codex is running in the session terminal. Web UI messages are "
            "forwarded into the same native Codex app-server thread."
        ),
        "executor": executor,
        # Opt the native session into the child-session spawn writes
        # (sys_session_create / sys_session_send / sys_session_close)
        # so the wrapped codex can author agent configs and launch
        # them as sub-agent sessions. The relay derives its advertised
        # tool set from this spec via ToolManager.
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped codex (the relay's
        # gate is a non-empty ``terminals:`` block on this spec).
        # Caller process / no sandbox matches the ``os_env`` stance
        # above — the native CLI already runs unsandboxed on the
        # user's workspace.
        "terminals": {
            "shell": {
                "command": "bash",
                "allow_cwd_override": True,
                "os_env": {
                    "type": "caller_process",
                    "cwd": ".",
                    "sandbox": {"type": "none"},
                },
            },
        },
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path

def _run_with_local_server(
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    codex_args: tuple[str, ...],
    command: str,
    model: str | None,
    prompt: str | None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Start a local Omnigent server, launch Codex, and attach to it.

    :param spec_path: Generated Codex wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the Codex-native picker.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable to run.
    :param model: Optional Codex model id.
    :param prompt: Optional first prompt.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :returns: None.
    """
    from omnigent.chat import (
        _bundle_agent,
        _find_free_port,
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    port = _find_free_port()
    server_handle = _start_local_server(spec_path, port, ephemeral=False)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(port, server_handle)
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers={},
            session_id=session_id,
            resume_picker=resume_picker,
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            return
        if resolved_session_id is not None:
            _align_working_directory_with_session(resolved_session_id)

        async def _drive() -> None:
            """
            Prepare Codex and attach in a single event loop.

            :returns: None.
            """
            with runner_startup_progress(initial_message="Preparing Codex...") as progress:
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_codex_terminal(
                    base_url=base_url,
                    headers={},
                    session_id=resolved_session_id,
                    runner_id=server_handle.runner_id,
                    session_bundle=bundle,
                    codex_args=codex_args,
                    command=command,
                    model=model,
                    startup_progress=progress,
                )
            if resolved_session_id is None:
                _record_launch_for_fresh_session(prepared.session_id)
            click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
            open_conversation_link_if_enabled(
                base_url=base_url,
                conversation_id=prepared.session_id,
                enabled=auto_open_conversation,
                warn=lambda message: click.echo(message, err=True),
            )
            await _attach_with_forwarder(
                base_url=base_url,
                headers={},
                prepared=prepared,
                prompt=prompt,
            )
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="codex",
                    session_id=prepared.session_id,
                )

        asyncio.run(_drive())
    finally:
        _stop_local_server(server_handle)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _rollout as _sib_rollout
    from . import _session_items as _sib_session_items
    from . import _terminal as _sib_terminal
    from . import _types as _sib_types
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
    for _key, _value in _sib_rollout.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_session_items.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
