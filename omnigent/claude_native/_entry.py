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

def build_native_claude_terminal_env(
    claude_config: ClaudeNativeUcodeConfig | None,
) -> dict[str, str]:
    """
    Build env overrides for a native Claude Code terminal process.

    Forces MCP Tool Search on so Claude defers MCP tool schemas and
    loads them on demand, and disables Claude Code's agent view so the
    terminal stays pinned to the session the Omnigent UI is showing.

    :param claude_config: Optional provider/ucode launch config, e.g.
        one carrying ``{"ANTHROPIC_BASE_URL": "https://example.com"}``.
        ``None`` means use Claude Code's own native auth.
    :returns: Environment overrides for the terminal process, e.g.
        ``{"ENABLE_TOOL_SEARCH": "true"}``.
    """
    terminal_env = {
        _CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV: "true",
        _CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV: "1",
    }
    if claude_config is not None:
        terminal_env.update(claude_config.env)
        terminal_env[_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV] = "true"
        terminal_env[_CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV] = "1"
    return terminal_env

def run_claude_native(
    *,
    server: str | None,
    session_id: str | None,
    claude_args: tuple[str, ...],
    resume_picker: bool = False,
    command: str = _DEFAULT_CLAUDE_COMMAND,
    use_claude_config: bool = False,
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Launch Claude Code in an Omnigent terminal and attach locally.

    :param server: Optional remote Omnigent server URL. ``None`` starts a
        local Omnigent server using the existing chat server machinery.
    :param session_id: Optional existing session to bind and reuse,
        e.g. ``"conv_abc123"``. ``None`` creates a new bundled
        session.
    :param claude_args: Args after ``claude``, e.g.
        ``("--dangerously-skip-permissions",)``. Stray ``--resume`` /
        ``-r`` is stripped defensively (Omnigent owns resume).
    :param resume_picker: ``True`` runs the claude-native picker
        once the server is reachable; ``False`` keeps the existing
        ``session_id``-or-fresh-session behavior.
    :param command: Executable to run in the terminal resource,
        e.g. ``"claude"``. Kept off the public CLI surface so v0
        always exposes Claude Code, while tests can supply a fake
        executable.
    :param use_claude_config: When ``True``, skip Databricks/ucode auth
        and let Claude use its own existing ``~/.claude/`` configuration.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :param startup_profiler: Optional shared startup profiler from the
        Click command. ``None`` creates one from
        ``OMNIGENT_CLAUDE_STARTUP_PROFILE``.
    :returns: None after the attach session ends.
    :raises click.ClickException: If setup, launch, or attach fails.
    """
    startup_profiler = startup_profiler or StartupProfiler.from_env(
        name="omnigent claude",
        env_var=_CLAUDE_STARTUP_PROFILE_ENV_VAR,
    )
    startup_profiler.mark("native launch entered")
    resolved_command = command.strip()
    if not resolved_command:
        raise click.ClickException("Claude command must not be empty.")
    startup_profiler.mark("checking local tools")
    _preflight_local_tools(resolved_command)
    startup_profiler.mark("local tools ready")
    sanitized_args = _strip_resume_from_claude_args(claude_args)
    startup_profiler.mark("claude args normalized")
    # Resolve the launch config across all offerings: a configured provider
    # (configure harnesses), the Databricks ucode profile, or Claude's own
    # login — so `omnigent claude` honors the provider selection just like
    # the in-process claude-sdk harness. ``use_claude_config`` forces the
    # CLI's own ~/.claude config (skips all of it).
    startup_profiler.mark("resolving claude config")
    claude_config = None if use_claude_config else resolve_native_claude_config(spec=None)
    startup_profiler.mark(
        "claude config resolved",
        detail="native config" if claude_config is not None else "claude cli config",
    )

    with TemporaryDirectory(prefix="omnigent-claude-native-") as tmpdir:
        spec_path = _materialize_claude_agent_spec(Path(tmpdir))
        startup_profiler.mark("agent spec materialized")
        if server is None:
            _run_with_local_server(
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                claude_args=sanitized_args,
                command=resolved_command,
                claude_config=claude_config,
                auto_open_conversation=auto_open_conversation,
                startup_profiler=startup_profiler,
            )
        else:
            # The daemon-spawned runner launches ``claude`` itself and
            # derives the ucode config from the provider config, so the
            # remote path takes neither ``command`` nor ``claude_config``.
            _run_with_remote_server(
                server.rstrip("/"),
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                claude_args=sanitized_args,
                auto_open_conversation=auto_open_conversation,
                startup_profiler=startup_profiler,
            )

def resolve_native_claude_config(
    *,
    spec: AgentSpec | None,
) -> ClaudeNativeUcodeConfig | None:
    """Resolve the native Claude Code launch config across all offerings.

    The single entry point both native-claude launch paths use (the CLI
    ``omnigent claude`` and the runner's host-spawned auto-create), so the
    native harness honors ``omnigent setup`` exactly like the in-process
    claude-sdk harness. Precedence mirrors
    :func:`omnigent.runtime.workflow._resolve_provider_for_build`:

    1. when a *spec* is given, its resolved provider (spec ``executor.auth``
       → explicit per-family default → global ``auth:`` → ``databricks-*``
       model → ambient detection), falling back to the spec's own
       ``executor.profile`` (ucode) when it routed to legacy databricks;
    2. when spec-less (``omnigent claude``): an explicit per-family default
       → global ``auth:`` (→ ucode) → ambient detection;
    3. otherwise ``None`` (Claude's own login).

    Credentials are controlled exclusively by the spec or by
    ``omnigent setup`` provider config — there is no CLI/env profile
    override.

    :param spec: The agent spec, or ``None`` for the bare ``omnigent
        claude`` launch.
    :returns: The launch config, or ``None`` to use Claude's own login.
    """
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )
    from omnigent.runtime.workflow import _load_global_auth, _resolve_provider_for_build
    from omnigent.spec.types import DatabricksAuth

    # 1. Spec-driven: reuse the harness routing precedence verbatim. A
    #    non-None entry decides the config (including a deliberate None for a
    #    subscription); a None entry means the spec routed to databricks /
    #    global auth → fall back to the spec's own ucode profile.
    if spec is not None:
        entry = _resolve_provider_for_build(spec, harness_type="claude-sdk")
        if entry is not None:
            return _native_claude_config_from_entry(entry)
        return _ucode_config_for_profile(spec.executor.profile)

    # 2. Spec-less (omnigent claude): explicit default wins first.
    explicit = load_config()
    entry = default_provider_for_harness(explicit, "claude-sdk")
    if entry is not None:
        return _native_claude_config_from_entry(entry)
    # A global databricks auth block → ucode.
    global_auth = _load_global_auth()
    if isinstance(global_auth, DatabricksAuth):
        return _ucode_config_for_profile(global_auth.profile)
    if global_auth is not None:
        # A global api_key auth: let Claude's own login handle it (parity
        # with the subscription path); the in-process harness would inject
        # it, but the native CLI uses its configured account.
        return None
    # 3. Ambient detection (first run without configure).
    entry = default_provider_for_harness(effective_config_with_detected(explicit), "claude-sdk")
    if entry is not None:
        return _native_claude_config_from_entry(entry)
    _logger.info(
        "native-claude routing: Claude CLI login (no provider configured for the Claude "
        "harness, no Databricks profile). Run `omnigent setup --no-internal-beta` to route "
        "through a provider."
    )
    return None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _helpers as _sib_helpers
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
