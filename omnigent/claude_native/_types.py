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

@dataclass(frozen=True)
class _ResumeWorkspaceActionOption:
    """
    One selectable action in the cwd-mismatch prompt.

    :param action: Stable action value returned to the caller, e.g.
        ``"switch"``.
    :param label: User-facing action label, e.g.
        ``"Switch working directory to /home/me/repo"``.
    """

    action: str
    label: str

@dataclass
class _ResumeWorkspaceActionPickerState:
    """
    Mutable state for the prompt-toolkit workspace action picker.

    :param options: Selectable workspace actions in display order.
    :param selected_index: Zero-based selected option index.
    """

    options: list[_ResumeWorkspaceActionOption]
    selected_index: int = 0

    def move_selection(self, delta: int) -> None:
        """
        Move the selected option up or down.

        :param delta: Signed row delta, e.g. ``1`` for down.
        :returns: None.
        """
        last_index = len(self.options) - 1
        self.selected_index = max(0, min(last_index, self.selected_index + delta))

    def selected_action(self) -> str:
        """
        Return the currently highlighted action.

        :returns: Action value, e.g. ``"move"``.
        """
        return self.options[self.selected_index].action

@dataclass(frozen=True)
class PreparedClaudeTerminal:
    """
    Prepared native Claude terminal attachment details.

    :param session_id: Omnigent session/conversation id.
    :param terminal_id: Terminal resource id to attach.
    :param bridge_dir: Filesystem bridge directory shared with
        Claude hooks/MCP helpers.
    :param reattached: ``True`` when the terminal already existed and
        was reused rather than launched in this invocation. Drives
        teardown ownership: a reattached invocation must not close
        the terminal on exit, because the launcher that originally
        created it owns its lifecycle.
    :param cold_resumed: ``True`` when we launched a fresh terminal
        against an existing Omnigent session (i.e. ``--resume <conv>`` with
        no live terminal). The forwarder must seek to the current
        transcript end in this case — when ``--resume <claude_sid>``
        is injected into the launch args, Claude reopens the prior
        JSONL transcript, and re-reading it from offset 0 would
        re-post every prior turn to AP. There is no server-side dedup:
        seeking to the end (plus the forwarder's persisted byte offset
        on subsequent ticks) is the only thing keeping old turns from
        being re-posted as new messages. ``cold_resumed`` is
        *independent* of ``reattached``: cold resume creates a new
        terminal (we own teardown) but the forwarder still needs the
        skip-existing behavior.
    :param tmux_socket: Runner tmux server socket path when the
        terminal exposed one and it is reachable from this process,
        e.g. ``Path("/tmp/omnigent-501/.../tmux.sock")``. ``None``
        when the runner did not advertise a socket. Drives the
        same-machine direct ``tmux attach`` fast path; a remote
        runner's socket won't exist locally, so the attach falls back
        to the WebSocket PTY bridge.
    :param tmux_target: tmux ``-t`` target for the terminal pane,
        e.g. ``"main"``. ``None`` when unavailable. Paired with
        ``tmux_socket`` for the direct attach.
    """

    session_id: str
    terminal_id: str
    bridge_dir: Path
    reattached: bool
    cold_resumed: bool = False
    tmux_socket: Path | None = None
    tmux_target: str | None = None

@dataclass(frozen=True)
class ClaudeNativeUcodeConfig:
    """
    Ucode-derived Claude Code launch configuration.

    :param env: Allowlisted environment variables for the ``claude``
        terminal process, e.g. ``{"ANTHROPIC_BASE_URL":
        "https://example.databricks.com/ai-gateway/anthropic"}``.
    :param api_key_helper: Claude Code ``apiKeyHelper`` command from
        ucode state, e.g. ``"databricks auth token --host
        https://example.databricks.com ..."``.
    :param model: Optional model id from ucode state, e.g.
        ``"databricks-claude-opus-4-7"``.
    """

    env: dict[str, str]
    api_key_helper: str
    model: str | None = None

class _AttachOutcome(Enum):
    """How a local Claude attach session ended.

    Distinguishes a user *detach* (tmux still alive, the runner should
    keep serving the web UI) from a real exit so the remote launcher
    can decide whether to tear the local runner down.

    :cvar EXITED: The user quit (stdin EOF / Ctrl-D), the terminal is
        gone, or the WS closed for a reason that ends the session. The
        launcher tears down the runner and Omnigent terminal resource.
    :cvar DETACHED: The user detached from tmux (close code 4405). The
        tmux session — and therefore Claude — is still running; the
        launcher adopts the runner so it outlives the local CLI and the
        web UI stays connected.
    """

    EXITED = "exited"
    DETACHED = "detached"

@dataclass(frozen=True)
class _ClaudeTerminalTmux:
    """
    Local tmux coordinates for a Claude terminal resource.

    :param socket: tmux server socket path the runner advertised in
        the terminal resource metadata, e.g.
        ``Path("/tmp/omnigent-501/.../tmux.sock")``. ``None`` when
        absent.
    :param target: tmux ``-t`` target, e.g. ``"main"``. ``None`` when
        absent.
    """

    socket: Path | None
    target: str | None


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
    from . import _terminal as _sib_terminal
    from . import _transcript as _sib_transcript
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
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
