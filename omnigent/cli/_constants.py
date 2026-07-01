"""CLI entry point for omnigent."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias, cast

import click
import yaml
from pydantic import BaseModel, ConfigDict
from rich import box
from rich.console import Console
from rich.table import Table

from omnigent._startup_profile import StartupProfiler
from omnigent.cli_sandbox import lakebox as _lakebox_alias_group
from omnigent.cli_sandbox import sandbox as _sandbox_group
from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.local_server import (
    _DEFAULT_LOCAL_PORT,
    _pid_alive,
    ensure_local_omnigent_server,
    local_server_status,
    local_server_url_if_healthy,
    server_config_signature,
    stop_local_omnigent_server,
    stop_untracked_local_server,
)
from omnigent.onboarding.sandboxes import available_providers as _sandbox_providers
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    import httpx

    from omnigent._runner_startup import RunnerStartupProgress
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.provider_config import ProviderEntry


# Any: YAML configs have heterogeneous value types (str, int, list, etc.)
_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_GLOBAL_CONFIG_PATH: Path = Path.home() / ".omnigent" / "config.yaml"
_STATE_DIR: Path = Path.home() / ".omnigent"
_LEGACY_STATE_DIRS: tuple[Path, ...] = (
    Path.home() / ".omnigents",
    Path.home() / ".omniagents",
)
_DATA_DIR_ENV_VAR = "OMNIGENT_DATA_DIR"
_LOCAL_CONFIG_RELPATH: Path = Path(".omnigent") / "config.yaml"
_AUTO_OPEN_CONVERSATION_CONFIG_KEY = "auto_open_conversation"
_GLOBAL_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "default_agent",
        "harness",
        "model",
        "server",
        _AUTO_OPEN_CONVERSATION_CONFIG_KEY,
    }
)
_BOOLEAN_CONFIG_KEYS: frozenset[str] = frozenset({_AUTO_OPEN_CONVERSATION_CONFIG_KEY})
_CONFIG_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_CONFIG_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})
# Names of every subcommand the click group owns. Used by the console-script
# entrypoint to reject removed top-level ad-hoc chat before click reports an
# opaque "no such command" error. Keep in sync with command registration.
_CLICK_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "attach",
        "claude",
        "codex",
        "config",
        "debby",
        "debug",
        "host",
        "lakebox",
        "login",
        "pane-picker",
        "pane-split",
        "pi",
        "polly",
        "resume",
        "run",
        "sandbox",
        "server",
        "setup",
        "stop",
        "upgrade",
        "version",
    }
)
_ConfigValue: TypeAlias = (
    str | int | float | bool | None | list["_ConfigValue"] | dict[str, "_ConfigValue"]
)
_GLOBAL_AGENTS_DIR: Path = Path.home() / ".omnigent" / "agents"
_INTERNAL_BETA_DEFAULT_AGENT_NAME: str = "databricks_coding_agent.yaml"
_INTERNAL_BETA_BUNDLED_AGENTS: tuple[str, ...] = (
    "databricks_coding_agent.yaml",
    "knowledge_work_agent.yaml",
)
_CLAUDE_STARTUP_PROFILE_ENV_VAR = "OMNIGENT_CLAUDE_STARTUP_PROFILE"
_HOST_DAEMON_STOP_GRACE_S = 5.0
_UPGRADE_DRAIN_POLL_S = 2.0
_DAEMON_RECONNECT_GRACE_S = 5.0
_DAEMON_REUSE_MIN_AGE_S = 6.0
_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT = 30
_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S = int(
    os.environ.get(
        "OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S",
        str(_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT),
    )
)
_LOCAL_DAEMON_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OMNIGENT_DATABASE_URI",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "PERPLEXITY_API_KEY",
        "TOGETHER_API_KEY",
        "VOYAGE_API_KEY",
        "XAI_API_KEY",
    }
)
_LOCAL_DAEMON_ENV_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_DEFAULT_",
    "AZURE_OPENAI_",
    "DATABRICKS_",
    "OMNIGENT_",
    "OPENAI_",
)
_HostJsonValue: TypeAlias = (
    str | int | float | bool | None | list["_HostJsonValue"] | dict[str, "_HostJsonValue"]
)
_HostJsonObject: TypeAlias = dict[str, _HostJsonValue]
_HostSessionRow: TypeAlias = dict[str, _HostJsonValue]
_HostPayload: TypeAlias = dict[str, _HostJsonValue]
_HOST_PID_PATH = Path.home() / ".omnigent" / "host.pid"
# host.pid records the daemon PID + the "target" it serves: a normalized
# server URL for remote/explicit targets, or the literal marker ``"local"``
# for a daemon that owns a local Omnigent server. Daemon reuse is keyed on this
# target (real URLs never collide with the marker).
_LOCAL_DAEMON_MARKER = "local"
_LOCAL_SERVER_DISCOVER_TIMEOUT_S = 120.0
# Click ``flag_value`` for bare ``--resume`` (no arg). Must exist
# before any command's decorator evaluates.
_RESUME_PICKER_SENTINEL = "__resume_picker__"
_HARNESS_CHOICES_HELP = (
    "'claude' (alias for 'claude-sdk'), 'claude-sdk', 'codex', "
    "'cursor', "
    "'openai-agents', 'open-responses', or 'pi'"
)
_HARNESS_HELP = f"Harness to use for a local agent: {_HARNESS_CHOICES_HELP}."
_RUN_HARNESS_HELP = (
    f"Harness to use: {_HARNESS_CHOICES_HELP}. Without AGENT, launches that harness directly."
)
_MODEL_HELP = "Model to use for the agent."
_PROMPT_HELP = "Send this as the first message when the REPL starts."
_SYSTEM_PROMPT_HELP = "Instructions to use for the agent."
_RESUME_HELP = (
    "Resume a prior conversation. With no value, opens an interactive "
    "picker; with a conversation id (e.g. --resume conv_abc123), attaches "
    "directly to that conversation."
)
_CONTINUE_HELP = "Continue the most recent conversation for this agent."
_NO_SESSION_HELP = "Use a fresh temporary local session store for this run."
_FORK_HELP = "Fork an existing session by id and open the REPL on the fork."
_LOG_HELP = "Write a JSON dump of the conversation to ~/.omnigent/logs/ on exit."
_DEFAULT_HARNESS_PROMPTS = {
    "claude-sdk": (
        "You are Claude Code, running through Omnigent. "
        "Help the user with software engineering tasks."
    ),
    "codex": (
        "You are Codex, running through Omnigent. Help the user with software engineering tasks."
    ),
    "cursor": (
        "You are Cursor, running through Omnigent. Help the user with software engineering tasks."
    ),
}
_DEFAULT_HARNESS_PROMPT = "You are a helpful coding agent running through Omnigent."
_OS_ENV_HARNESSES: frozenset[str] = frozenset({"claude-sdk", "codex", "pi"})
_NODE_MIN_VERSION_HINT = "Node.js 22 LTS or newer (a 22.10+ API is required)"
_CLI_LOGIN_TIMEOUT_SECONDS = 300  # 5 minutes
_PANE_SPLIT_DIRECTIONS = ("v", "h", "w")
