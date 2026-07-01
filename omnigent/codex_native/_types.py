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

@dataclass(frozen=True)
class _ResumeWorkspaceActionOption:
    """
    One selectable action in the Codex cwd-mismatch prompt.

    :param action: Stable action value returned to the caller, e.g.
        ``"switch"``.
    :param label: User-facing action label, e.g.
        ``"Switch working directory to /home/me/repo"``.
    """

    action: str
    label: str

@dataclass
class LaunchedCodexTerminal:
    """
    Terminal resource returned by the Omnigent runner launch path.

    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_codex_main"``.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one, e.g. ``"/tmp/omnigent-terminal-x/tmux.sock"``.
    :param tmux_target: Tmux target when exposed by the runner,
        e.g. ``"main"``.
    """

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None

@dataclass
class PreparedCodexTerminal:
    """
    Prepared native Codex terminal attachment details.

    :param session_id: Omnigent session/conversation id.
    :param terminal_id: Terminal resource id to attach.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one and it is reachable from this CLI process.
    :param tmux_target: Tmux target for direct local attaches, e.g.
        ``"main"``.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex app-server thread id. ``None`` until the
        first attached TUI creates a fresh thread.
    :param app_server_url: App-server transport the TUI, forwarder, and
        initial-turn connect over, e.g. ``"ws://127.0.0.1:9876"``. ``None``
        for runner-owned terminal attaches where the CLI never connects to
        the app-server directly.
    :param app_server: Running app-server process when this wrapper
        invocation owns it. ``None`` for reattached live terminals.
    :param event_client: App-server client already listening for the
        Codex thread. Fresh sessions keep this listener open after it
        observes the TUI-created ``thread/started`` event.
    :param reattached: ``True`` when an existing terminal was reused.
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    bridge_dir: Path
    thread_id: str | None
    app_server_url: str | None
    app_server: CodexNativeAppServer | None
    event_client: CodexAppServerClient | None
    reattached: bool


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _rollout as _sib_rollout
    from . import _session_items as _sib_session_items
    from . import _terminal as _sib_terminal
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
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
