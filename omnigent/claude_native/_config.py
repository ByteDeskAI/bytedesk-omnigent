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

def _ucode_config_for_profile(profile: str | None) -> ClaudeNativeUcodeConfig | None:
    """
    Resolve native Claude Code launch config from ucode state.

    The profile remains the explicit workspace selector. If no
    profile is selected, or the profile has no matching ucode state,
    the native wrapper leaves Claude Code's normal provider
    configuration alone.

    :param profile: Databricks CLI profile name, e.g.
        ``"<your-profile>"``.
    :returns: Ucode-derived launch config, or ``None`` when no matching
        ucode state exists.
    :raises click.ClickException: If the selected workspace has a
        malformed Claude ucode agent entry.
    """
    if not profile:
        return None

    from omnigent.onboarding.databricks_config import (
        DATABRICKS_CLAUDE_DEFAULT_MODEL,
        get_workspace_url_for_profile,
    )
    from omnigent.onboarding.ucode_state import read_ucode_state

    workspace_url = get_workspace_url_for_profile(profile)
    if workspace_url is None:
        return None
    workspace_state = read_ucode_state(workspace_url)
    if workspace_state is None:
        return None
    agent_state = workspace_state.agent(_UCODE_CLAUDE_AGENT_NAME)
    if agent_state is None:
        raise click.ClickException(
            f"ucode state for profile {profile!r} does not include a Claude agent entry. "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )

    base_url = agent_state.env.get(_UCODE_CLAUDE_BASE_URL_ENV) or agent_state.base_url
    if base_url is None:
        base_url = agent_state.base_urls.get(_UCODE_CLAUDE_AGENT_NAME)
    if not base_url:
        raise click.ClickException(
            f"ucode state for profile {profile!r} is missing Claude base URL "
            f"({_UCODE_CLAUDE_BASE_URL_ENV} / base_url). "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )
    if not agent_state.auth_command:
        raise click.ClickException(
            f"ucode state for profile {profile!r} is missing Claude auth_command. "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )

    refresh_interval_ms = (
        agent_state.auth_refresh_interval_ms or _DEFAULT_UCODE_AUTH_REFRESH_INTERVAL_MS
    )
    env: dict[str, str] = {
        _UCODE_CLAUDE_BASE_URL_ENV: base_url,
        _CLAUDE_CODE_API_KEY_HELPER_TTL_ENV: str(refresh_interval_ms),
        _CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV: "1",
    }
    # Pin each Claude Code model-tier alias to the corresponding Databricks
    # gateway model ID so that the /model picker natively shows gateway model
    # names.  Without this Claude Code normalises the picked model to a
    # canonical Anthropic name (e.g. "claude-opus-4-7[1m]") that the
    # Databricks gateway rejects.
    for tier, env_var in _UCODE_CLAUDE_TIER_TO_ENV.items():
        model_id = workspace_state.claude_models.get(tier)
        if model_id:
            env[env_var] = model_id
    # When ucode caches no model, default it so Claude Code doesn't fall back
    # to its host-config model (an Anthropic-direct id the gateway rejects).
    return ClaudeNativeUcodeConfig(
        env=env,
        api_key_helper=agent_state.auth_command,
        model=agent_state.model or DATABRICKS_CLAUDE_DEFAULT_MODEL,
    )

def _provider_config_for_native_claude(entry: ProviderEntry) -> ClaudeNativeUcodeConfig | None:
    """Build native Claude Code launch config from a generic provider.

    The OSS counterpart to :func:`_ucode_config_for_profile`: it takes a
    resolved ``key`` / ``gateway`` / ``local`` provider serving the
    ``anthropic`` surface and injects the same knobs the native CLI needs —
    ``ANTHROPIC_BASE_URL`` plus a token ``apiKeyHelper`` and the default
    model — so a Claude Code terminal launched by ``omnigent`` routes
    through the configured provider exactly like the in-process claude-sdk
    harness does (:func:`omnigent.runtime.workflow.configure_agent_harness_with_provider`).

    :param entry: A resolved provider entry. Only ``key`` / ``gateway`` /
        ``local`` kinds serving the ``anthropic`` family produce a config.
    :returns: The launch config, or ``None`` when the provider does not
        serve the anthropic surface or carries no usable credential (the
        caller then falls back to the CLI's own login).
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

    family = entry.family(ANTHROPIC_FAMILY)
    if family is None:
        _logger.warning(
            "native-claude: provider %r is the Claude default but does not serve the "
            "anthropic surface — falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    # Token delivery mirrors the claude-sdk executor: a dynamic auth_command
    # is used verbatim; a static key becomes a ``printf`` apiKeyHelper (the
    # runner env allowlist excludes ANTHROPIC_API_KEY, so the key must reach
    # Claude Code via the helper, not the environment).
    if family.auth_command:
        api_key_helper = family.auth_command
    elif family.api_key:
        api_key_helper = f"printf %s {shlex.quote(family.api_key)}"
    else:
        _logger.warning(
            "native-claude: provider %r is the Claude default but has no usable "
            "credential — falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    _logger.info(
        "native-claude routing: provider %r (base_url=%s, model=%s)",
        entry.name,
        family.base_url,
        family.default_model,
    )
    return ClaudeNativeUcodeConfig(
        env={
            _UCODE_CLAUDE_BASE_URL_ENV: family.base_url,
            # Disable Claude Code's experimental anthropic-beta flags. Gateways
            # (Databricks serving-endpoints and the like) reject beta flags they
            # don't implement with a 400 "invalid beta flag", which kills every
            # turn. The ucode/databricks path already sets this; mirror it here
            # so the generic key/gateway/local provider path is equally
            # gateway-safe.
            _CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV: "1",
        },
        api_key_helper=api_key_helper,
        model=family.default_model,
    )

def _native_claude_config_from_entry(
    entry: ProviderEntry,
) -> ClaudeNativeUcodeConfig | None:
    """Map a resolved provider entry to a native Claude launch config.

    - ``key`` / ``gateway`` / ``local`` → provider gateway config
      (:func:`_provider_config_for_native_claude`).
    - ``databricks`` → the existing ucode path keyed on the provider profile.
    - ``subscription`` → ``None`` (use the ``claude`` CLI's own login, e.g. a
      Claude Enterprise seat) — intentional, not a fallback to ucode.

    :param entry: The resolved provider entry.
    :returns: The launch config, or ``None`` to use Claude's own login.
    """
    from omnigent.onboarding.provider_config import (
        DATABRICKS_KIND,
        GATEWAY_KIND,
        KEY_KIND,
        LOCAL_KIND,
    )

    if entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        return _provider_config_for_native_claude(entry)
    if entry.kind == DATABRICKS_KIND:
        _logger.info("native-claude routing: Databricks ucode profile %r", entry.profile)
        return _ucode_config_for_profile(entry.profile)
    _logger.info("native-claude routing: Claude CLI login (subscription provider %r)", entry.name)
    return None

def _materialize_claude_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first session agent spec used by ``omnigent claude``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to a generated YAML spec.
    """
    yaml_path = tmpdir / "claude-native-ui.yaml"
    raw = {
        "name": _AGENT_NAME,
        "prompt": (
            "Claude Code is running in the session terminal. Web UI messages are "
            "forwarded into that Claude Code process through the native bridge."
        ),
        "executor": {
            "harness": "claude-native",
            # Conservative pre-first-turn default; the forwarder
            # overrides it via ``external_session_usage`` once the
            # real model + ``[1m]`` alias are observed.
            "context_window": 200_000,
        },
        # Opt the native session into the child-session spawn writes
        # (sys_session_create / sys_session_send / sys_session_close)
        # so the wrapped Claude Code can author agent configs and
        # launch them as sub-agent sessions. The relay derives its
        # advertised tool set from this spec via ToolManager.
        "spawn": True,
        # Without an ``os_env`` block, the runner's filesystem APIs
        # (``/resources/environments/default/filesystem`` and siblings)
        # return 404 — see ``_require_os_env`` in
        # ``omnigent/runner/app.py``. Claude Code already operates
        # on the user's workspace with full filesystem access, so the
        # caller process / no sandbox combination matches reality and
        # enables the web UI's files panel.
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped Claude Code (the
        # relay's gate is a non-empty ``terminals:`` block on this
        # spec). Caller process / no sandbox matches the ``os_env``
        # stance above — the native CLI already runs unsandboxed on
        # the user's workspace.
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
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return yaml_path


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
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
