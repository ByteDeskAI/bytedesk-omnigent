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


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _is_claude_native_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> bool:
    """
    Return whether *conversation_id* is a claude-native wrapper session.

    :param base_url: Omnigent server base URL.
    :param conversation_id: Omnigent conversation id.
    :returns: ``True`` only when the wrapper label matches Claude native.
    """
    return (
        _wrapper_label_for_conversation(
            base_url=base_url,
            conversation_id=conversation_id,
        )
        == _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE
    )

def _redirect_native_resume_if_needed(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None = None,
) -> bool:
    """
    Redirect a terminal-native resume before Omnigent attach liveness runs.

    :param base_url: Omnigent server base URL, e.g. ``"https://example.com"``.
    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional startup spinner to finish before redirect.
    :returns: ``True`` when a native wrapper handled the resume.
    """
    wrapper_label = _wrapper_label_for_conversation(
        base_url=base_url, conversation_id=conversation_id
    )
    native_agent = native_coding_agent_for_wrapper_label(wrapper_label)
    if native_agent is None:
        return False
    if native_agent.key == "claude":
        _run_claude_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    if native_agent.key == "codex":
        _run_codex_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    if native_agent.key == "pi":
        _run_pi_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    return False

def _finish_native_redirect_progress(
    *,
    progress: RunnerStartupProgress | None,
    conversation_id: str,
    wrapper_name: str,
    native_command: str,
) -> None:
    """
    Finish any Omnigent startup progress and print the native redirect notice.

    :param progress: Optional startup spinner to finish before writing.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param wrapper_name: Wrapper label for display, e.g. ``"codex-native"``.
    :param native_command: Native command to show, e.g. ``"codex"``.
    :returns: None.
    """
    if progress is not None:
        progress.finish()
    click.echo(
        (
            f"\n  Conversation {conversation_id} is a {wrapper_name} "
            f"session — redirecting to `omnigent {native_command} --resume`.\n"
        ),
        err=True,
    )

def _run_claude_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a claude-native conversation back to ``omnigent claude``.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="claude-native",
        native_command="claude",
    )
    from omnigent.claude_native import run_claude_native

    run_claude_native(
        server=base_url,
        session_id=conversation_id,
        claude_args=(),
        auto_open_conversation=auto_open_conversation,
    )

def _run_codex_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a codex-native conversation back to ``omnigent codex``.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="codex-native",
        native_command="codex",
    )
    from omnigent.codex_native import run_codex_native

    run_codex_native(
        server=base_url,
        session_id=conversation_id,
        codex_args=(),
        auto_open_conversation=auto_open_conversation,
    )

def _run_pi_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a pi-native conversation back to ``omnigent pi``.

    :param base_url: Omnigent server base URL.
    :param conversation_id: Omnigent conversation id.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="pi-native",
        native_command="pi",
    )
    from omnigent.pi_native import run_pi_native

    run_pi_native(
        server=base_url,
        session_id=conversation_id,
        pi_args=(),
        auto_open_conversation=auto_open_conversation,
    )

def _wrapper_label_for_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> str | None:
    """
    Return a conversation's wrapper label, if it can be read.

    Single-shot ``GET /v1/sessions/{id}`` against *base_url*, inspecting
    the response's ``labels.omnigent.wrapper`` field. ``None`` on any
    transport / parse error so a flaky server doesn't silently misroute
    the resume — the caller falls back to the normal Omnigent REPL path and
    surfaces a clear failure there.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Omnigent conversation id,
        e.g. ``"conv_abc123"``.
    :returns: Wrapper label value, or ``None``.
    """
    try:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{conversation_id}",
            headers=_remote_headers(server_url=base_url),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "wrapper-label probe failed for %s on %s: %s",
            conversation_id,
            base_url,
            exc,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "wrapper-label probe got %s for %s on %s; treating as no wrapper",
            resp.status_code,
            conversation_id,
            base_url,
        )
        return None
    try:
        body = resp.json()
    except ValueError as exc:
        logger.warning(
            "wrapper-label probe for %s returned non-JSON body: %s",
            conversation_id,
            exc,
        )
        return None
    if not isinstance(body, dict):
        logger.warning(
            "wrapper-label probe for %s returned non-object body",
            conversation_id,
        )
        return None
    labels = body.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    return value if isinstance(value, str) else None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _sessions as _sib_sessions
    from . import _types as _sib_types
    for _key, _value in _sib_daemon.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overrides.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_repl.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_server_proc.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_sessions.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
