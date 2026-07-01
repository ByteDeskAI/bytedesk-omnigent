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
_AGENT_NAME = "claude-native-ui"
_DEFAULT_CLAUDE_COMMAND = "claude"
_CLAUDE_TERMINAL_SCROLLBACK_LINES = 100_000
_TERMINAL_NAME = "claude"
_TERMINAL_SESSION_KEY = "main"
_UCODE_CLAUDE_AGENT_NAME = "claude"
_UCODE_CLAUDE_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_CLAUDE_CODE_NESTED_SESSION_ENV = "CLAUDECODE"
_CLAUDE_CODE_API_KEY_HELPER_TTL_ENV = "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"
_CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV = "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"
_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"
_CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV = "CLAUDE_CODE_DISABLE_AGENT_VIEW"
_ANTHROPIC_DEFAULT_FABLE_MODEL_ENV = "ANTHROPIC_DEFAULT_FABLE_MODEL"
_ANTHROPIC_DEFAULT_OPUS_MODEL_ENV = "ANTHROPIC_DEFAULT_OPUS_MODEL"
_ANTHROPIC_DEFAULT_SONNET_MODEL_ENV = "ANTHROPIC_DEFAULT_SONNET_MODEL"
_ANTHROPIC_DEFAULT_HAIKU_MODEL_ENV = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
_DEFAULT_UCODE_AUTH_REFRESH_INTERVAL_MS = 900_000
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_CLAUDE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESUME_ACTION_SWITCH = "switch"
_RESUME_ACTION_MOVE = "move"
_RESUME_ACTION_LEAVE = "leave"
_ATTACH_INITIAL_RECONNECT_DELAY_S = 0.5
_ATTACH_MAX_RECONNECT_DELAY_S = 5.0
_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S = 0.25
_CLAUDE_TERMINAL_GONE_WATCH_INTERVAL_S = 0.25
_CLAUDE_TERMINAL_GONE_WATCH_HTTP_TIMEOUT_S = 1.0
_CLAUDE_STARTUP_PROFILE_ENV_VAR = "OMNIGENT_CLAUDE_STARTUP_PROFILE"

