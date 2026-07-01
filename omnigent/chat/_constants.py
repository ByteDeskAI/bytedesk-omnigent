"""Implementation of the ``omnigent chat`` command.

The CLI always ends by connecting an Omnigent client to a server URL. For
path targets it first ensures the agent is registered on that server
(a local subprocess by default, or ``--server`` when supplied). URL
targets skip setup and use the existing server's registered agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import httpx
import yaml
from omnigent_client import (
    OmnigentClient,
    SessionToolCallInfo,
    ToolCallable,
    ToolCallInfo,
    ToolHandler,
)
from omnigent_client import (
    OmnigentError as ClientOmnigentError,
)
from omnigent_client._events import (
    ErrorEvent,
    ResponseCancelled,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
)
from rich.console import Console

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)
from omnigent.conversation_browser import open_conversation_link_if_enabled
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.databricks_executor import _DatabricksBearerAuth, _read_databrickscfg
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label
from omnigent.spec import load as load_spec
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress

console = Console()

# YAML mapping shape — heterogeneous JSON-shaped values
# (strings, ints, lists, nested dicts) so ``Any`` is the
# narrowest safe element type. Used as the parsed-spec
# return / input shape across this module's helpers.
_YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

# Local server readiness polling: use a short initial interval so
# freshly-launched ``omnigent run`` sessions don't burn a
# fixed 500 ms before noticing the server is ready, then back off
# slightly while still remaining responsive on slower cold starts.
_SERVER_READY_INITIAL_POLL_SECONDS = 0.05
_SERVER_READY_BACKOFF_POLL_SECONDS = 0.1
_SERVER_READY_FAST_POLL_WINDOW_SECONDS = 1.0

# Remote ``--server`` runners are disposable subprocesses created for
# the CLI session. A one-second grace gives SIGTERM enough time to
# flush runner logs and unregister without noticeably slowing CLI exit.
# Grace period before the CLI escalates SIGTERM → SIGKILL on the
# runner subprocess. Must be long enough for the runner's shutdown
# chain to complete: cancel async tasks → app.router.shutdown() →
# _stop_pm() → _terminal_registry.shutdown() → tmux kill-server
# per session → pm.shutdown() → SIGTERM each harness. 1 s was too
# short — the runner was SIGKILL'd before tmux sessions were reaped,
# leaving zombie codex/claude processes.
_REMOTE_RUNNER_STOP_GRACE_SECONDS = 8.0

# Fallback model when the YAML declares neither ``executor.model``
# nor ``executor.harness`` AND no ``--model`` / ``--harness``
# override is supplied. Mirrors the legacy argparse CLI's
# ``_DEFAULT_AD_HOC_MODEL`` so ``omnigent run examples/hello_world.yaml``
# (a spec with no executor block) launches cleanly instead of
# failing the strict omnigent validator with a cryptic
# "executor.config.harness: required" error.
_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"

# How many of the NEWEST transcript items ``_persisted_turn_text``
# fetches when reconciling a headless ``-p`` turn against the durable
# store. The current turn's items are always the newest, and no single
# one-shot turn emits anywhere near this many items, so the latest turn
# is fully captured regardless of how long a resumed session's history
# is. Fetched ``order="desc"`` (newest first) precisely so the window
# tracks the end of the conversation, not its start.
_RECONCILE_ITEMS_LIMIT = 100

# Optional bearer token for remote omnigent servers that sit
# behind an auth proxy (for example Databricks Apps). When set, the
# CLI sends ``Authorization: Bearer <value>`` on every HTTP request it
# makes to the remote server.
_REMOTE_AUTH_TOKEN_ENV = "OMNIGENT_REMOTE_AUTH_TOKEN"

# Env-var override name. ``OMNIGENT_MODEL=foo`` lets a user
# pin a default model per shell session without needing to pass
# ``--model foo`` on every invocation. Resolved once at spec
# materialization time (not at runtime), so the materialized
# bundle stays self-contained — identical behavior on any host
# that runs the bundle, regardless of that host's env. Mirrors
# the legacy ``_default_cli_model`` at
# ``omnigent/inner/cli.py:344``.
_OMNIGENT_MODEL_ENV_VAR = "OMNIGENT_MODEL"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
_OPENAI_AGENTS_HARNESSES = frozenset({"openai-agents", "openai-agents-sdk"})
_MATERIALIZED_OVERRIDE_DIRS: dict[Path, Path] = {}


_DAEMON_CHAT_HOST_ONLINE_TIMEOUT_S = 30.0
_DAEMON_CHAT_RUNNER_ONLINE_TIMEOUT_S = 60.0
_ACCOUNTS_SETUP_POLL_INTERVAL_S = 1.0
_ACCOUNTS_SETUP_TIMEOUT_S = 600.0
_ResponseOutput: TypeAlias = list[dict[str, Any]]  # type: ignore[explicit-any]
_SERVER_LOG_TAIL_LINES = 50

