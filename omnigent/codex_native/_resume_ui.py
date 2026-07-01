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

def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """
    Show one concise Codex startup milestone when a renderer is active.

    :param startup_progress: Optional progress renderer from
        :func:`runner_startup_progress`.
    :param message: User-facing status text, e.g.
        ``"Starting Codex terminal..."``.
    :returns: None.
    """
    if startup_progress is not None:
        startup_progress.update(message)

def _record_launch_for_fresh_session(session_id: str) -> None:
    """
    Persist the wrapper's current cwd as the Codex session launch state.

    :param session_id: Newly created Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        write_launch_state(session_id, str(Path.cwd().resolve()))
    except OSError:
        _logger.warning(
            "failed to record codex-native launch state for %s",
            session_id,
            exc_info=True,
        )

def _align_working_directory_with_session(session_id: str) -> None:
    """
    Resolve cwd mismatch before resuming a Codex-native session.

    Native Codex state is workspace-scoped from the user's point of
    view: the app-server and TUI should reopen from the directory
    where the session was created. If client-side launch state is
    present and points at a different existing directory, ask whether
    to switch there before the runner and app-server sample cwd.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: None. Side-effect-only; may change process cwd.
    :raises click.ClickException: If recorded state exists but no
        viable resume directory exists, or if the user cancels.
    """
    state = read_launch_state(session_id)
    if state is None:
        return
    current = Path.cwd().resolve()
    recorded_path = Path(state.working_directory).resolve()
    if current == recorded_path:
        return
    if not recorded_path.is_dir():
        raise click.ClickException(
            f"Session {session_id} was created in {recorded_path}, but that "
            "directory no longer exists. Recreate or move the project back "
            "before resuming Codex."
        )
    action = _prompt_codex_resume_workspace_action(
        recorded_path=recorded_path,
        current=current,
    )
    if action == _RESUME_ACTION_SWITCH:
        _switch_to_recorded_working_directory(recorded_path)
        return
    raise click.ClickException("Resume cancelled.")

def _prompt_codex_resume_workspace_action(
    *,
    recorded_path: Path,
    current: Path,
) -> str:
    """
    Ask how to handle a Codex resume cwd mismatch.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: One of ``"switch"`` or ``"cancel"``.
    """
    options = _codex_resume_workspace_action_options(recorded_path=recorded_path)
    click.echo(f"\nSession was started in: {recorded_path}", err=True)
    click.echo(f"Current working directory: {current}", err=True)
    click.echo("Codex resume is workspace-scoped. Choose an action:", err=True)
    for option in options:
        click.echo(f"  {option.action:<6} - {option.label}", err=True)
    return click.prompt(
        "Resume action",
        type=click.Choice([option.action for option in options]),
        default=options[0].action,
        show_choices=True,
        err=True,
    )

def _codex_resume_workspace_action_options(
    *,
    recorded_path: Path,
) -> list[_ResumeWorkspaceActionOption]:
    """
    Build the valid actions for a cwd-mismatched Codex resume.

    :param recorded_path: Recorded launch cwd, already resolved.
    :returns: Action options in display order.
    """
    return [
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_SWITCH,
            label=f"Switch working directory to {recorded_path}",
        ),
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_CANCEL,
            label="Cancel resume",
        ),
    ]

def _switch_to_recorded_working_directory(recorded_path: Path) -> None:
    """
    Switch process cwd to *recorded_path* for Codex resume.

    :param recorded_path: Existing recorded launch cwd.
    :returns: None.
    """
    os.chdir(recorded_path)
    click.echo(f"Switched to {recorded_path}.", err=True)

def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """
    Translate resume inputs into a concrete Codex-native session id.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Explicit session id, e.g. ``"conv_abc123"``.
    :param resume_picker: ``True`` for bare ``--resume``.
    :returns: Session id, or ``None`` for a fresh session / cancelled
        picker.
    """
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
        """
        Run the async Codex-native picker.

        :returns: Selected Omnigent session id, or ``None``.
        """
        async with OmnigentClient(
            base_url=base_url,
            headers=headers if headers else None,
        ) as client:
            return await pick_conversation_by_wrapper_label_from_sdk(
                client,
                wrapper_value=_WRAPPER_LABEL_VALUE,
                agent_name=_AGENT_NAME,
            )

    return asyncio.run(_drive())


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
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
    for _key, _value in _sib_local_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote_server.__dict__.items():
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
