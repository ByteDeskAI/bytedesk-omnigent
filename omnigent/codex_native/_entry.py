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

def run_codex_native(
    *,
    server: str | None,
    session_id: str | None,
    codex_args: tuple[str, ...],
    resume_picker: bool = False,
    command: str = _DEFAULT_CODEX_COMMAND,
    model: str | None = None,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch Codex TUI in an Omnigent terminal.

    :param server: Resolved Omnigent server URL, e.g.
        ``"http://127.0.0.1:8123"``.
    :param session_id: Optional existing Omnigent conversation id,
        e.g. ``"conv_abc123"``.
    :param codex_args: Raw Codex CLI args to pass before ``resume``.
    :param resume_picker: ``True`` runs the Codex-native picker.
    :param command: Codex executable, e.g. ``"codex"``.
    :param model: Optional model id, e.g. ``"gpt-5.4-mini"``.
    :param prompt: Optional first prompt to send after launch.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :returns: None after the terminal attach session ends.
    :raises click.ClickException: If setup fails.
    """
    resolved_command = command.strip()
    if not resolved_command:
        raise click.ClickException("Codex command must not be empty.")
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Codex requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_codex_native."
        )
    with TemporaryDirectory(prefix="omnigent-codex-native-") as tmpdir:
        spec_path = _materialize_codex_agent_spec(Path(tmpdir), model=model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            codex_args=codex_args,
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )

def codex_terminal_resource_id() -> str:
    """
    Return the deterministic terminal resource id for Codex.

    :returns: Terminal resource id, e.g. ``"terminal_codex_main"``.
    """
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _rollout as _sib_rollout
    from . import _session_items as _sib_session_items
    from . import _terminal as _sib_terminal
    from . import _types as _sib_types
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
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
