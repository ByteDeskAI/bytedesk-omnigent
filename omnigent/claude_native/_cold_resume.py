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

async def _fetch_claude_session_labels(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, str]:
    """
    Fetch labels for an existing Claude-native Omnigent session.

    :param client: HTTP client for the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Session labels as a string dictionary. Empty when the
        session has no labels.
    :raises click.ClickException: If the session lookup fails.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    if resp.status_code == 404:
        raise click.ClickException(
            f"Conversation {session_id!r} not found on the server. "
            "Run `omnigent claude` (no --resume) to start a new session.",
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch conversation {session_id!r} "
            f"({resp.status_code}): {error_text(resp)}",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Conversation fetch returned non-JSON body: {exc}",
        ) from exc
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}

async def _resolve_cold_resume_args(
    client: httpx.AsyncClient,
    session_id: str,
) -> tuple[str, ...]:
    """
    Build the ``claude --resume <sid>`` args for a cold-resume launch.

    Looks up the claude session id captured into
    ``conversations.external_session_id`` and injects it so the new
    terminal reattaches to the prior claude transcript. Fails loud if
    the conversation isn't claude-native; warns and returns empty if
    no external session id was ever captured, or if synthesizing the
    local transcript yields no resumable records (an empty transcript
    would make ``claude --resume`` exit instead of start).

    :param client: HTTP client for the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: ``("--resume", "<claude_sid>")`` or ``()`` when no id is
        mapped or there is no resumable history.
    :raises click.ClickException: Conversation missing or not claude-native.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    if resp.status_code == 404:
        raise click.ClickException(
            f"Conversation {session_id!r} not found on the server. "
            "Run `omnigent claude` (no --resume) to start a new session.",
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch conversation {session_id!r} "
            f"({resp.status_code}): {error_text(resp)}",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Conversation fetch returned non-JSON body: {exc}",
        ) from exc
    labels = payload.get("labels") if isinstance(payload, dict) else None
    wrapper = labels.get(_WRAPPER_LABEL_KEY) if isinstance(labels, dict) else None
    if wrapper != _WRAPPER_LABEL_VALUE:
        raise click.ClickException(
            f"Conversation {session_id!r} is not a claude-native session "
            f"(wrapper={wrapper!r}). Use `omnigent run --resume "
            f"{session_id}` to resume it through the right runtime.",
        )
    external_session_id = payload.get("external_session_id")
    if not isinstance(external_session_id, str) or not external_session_id:
        # Omnigent conv survives; claude side starts fresh. Warn on
        # both channels: ``click.echo`` for the foreground user,
        # ``_logger.warning`` for log aggregation (Sentry).
        message = (
            f"claude session id was never captured for {session_id!r}; "
            f"resuming with no prior claude context."
        )
        click.echo(f"warning: {message}", err=True)
        _logger.warning(message)
        return ()
    transcript = await _ensure_local_claude_resume_transcript(
        client,
        session_id=session_id,
        external_session_id=external_session_id,
        workspace=Path.cwd().resolve(),
    )
    if transcript is None:
        # No resumable records: ``claude --resume`` against an empty (or
        # absent) transcript exits with "No conversation found" instead of
        # starting. Launch fresh — the Omnigent conv survives.
        message = (
            f"no resumable claude history for {session_id!r}; "
            f"resuming with no prior claude context."
        )
        click.echo(f"warning: {message}", err=True)
        _logger.warning(message)
        return ()
    return ("--resume", external_session_id)

async def _fetch_all_session_items_for_claude_resume(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Fetch committed session items in chronological order.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Flat API item dicts from
        ``GET /v1/sessions/{id}/items``.
    :raises click.ClickException: If an item page cannot be fetched or
        parsed.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 1000, "order": "asc"}
        if after is not None:
            params["after"] = after
        resp = await client.get(
            f"/v1/sessions/{url_component(session_id)}/items",
            params=params,
        )
        if resp.status_code >= 400:
            raise click.ClickException(
                f"Failed to fetch history for {session_id!r} "
                f"({resp.status_code}): {error_text(resp)}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise click.ClickException(
                f"History fetch for {session_id!r} returned non-JSON body: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise click.ClickException(
                f"History fetch for {session_id!r} returned an invalid item list."
            )
        for item in data:
            if isinstance(item, dict):
                items.append(item)
        if not payload.get("has_more"):
            return items
        last_id = payload.get("last_id")
        if not isinstance(last_id, str) or not last_id:
            raise click.ClickException(
                f"History fetch for {session_id!r} set has_more without last_id."
            )
        after = last_id


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _terminal as _sib_terminal
    from . import _transcript as _sib_transcript
    from . import _types as _sib_types
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
