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

def _is_url(target: str) -> bool:
    """
    Check if the target looks like a URL.

    :param target: The target string.
    :returns: True if it starts with http:// or https://.
    """
    return target.startswith(("http://", "https://"))

def _remote_headers(
    server_url: str | None = None,
) -> dict[str, str]:
    """
    Build headers for remote AP-server requests.

    Resolution order:
      1. explicit ``OMNIGENT_REMOTE_AUTH_TOKEN`` env var
      2. stored OIDC token from ``~/.omnigent/auth_tokens.json``
         (populated by ``omnigent login``)
      3. stored Databricks Apps pointer record for ``server_url``
         (populated by ``omnigent login <apps-url>``) — mints a
         fresh workspace OAuth token via the SDK
      4. ambient Databricks CLI / ``~/.databrickscfg`` credentials
         (the SDK's default resolution; no profile is threaded)

    This lets ``omnigent run --server <apps-url>`` work against
    Databricks Apps after a one-time ``omnigent login <apps-url>``,
    without forcing the user to manually copy a bearer into an env var.

    :param server_url: Optional remote server URL for looking up
        stored OIDC tokens, e.g. ``"http://localhost:6767"``.
    :returns: Headers to pass to httpx / OmnigentClient.
    """
    token = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
    if token and (token := token.strip()):
        return {"Authorization": f"Bearer {token}"}
    # Check stored OIDC token from `omnigent login`.
    if server_url:
        from omnigent.cli_auth import load_token

        oidc_token = load_token(server_url)
        if oidc_token:
            return {"Authorization": f"Bearer {oidc_token}"}
        record_token = _stored_databricks_record_token(server_url)
        if record_token:
            return {"Authorization": f"Bearer {record_token}"}
    creds = _read_databrickscfg(None)
    if creds is None or not creds.token:
        return {}
    return {"Authorization": f"Bearer {creds.token}"}

def _stored_databricks_record_token(server_url: str) -> str | None:
    """Mint a workspace token from a stored Databricks Apps record.

    ``omnigent login <apps-url>`` stores a pointer record naming the
    workspace that fronts the app; this resolves it to a fresh bearer
    via the Databricks CLI's host-keyed OAuth cache. One-shot — callers
    that issue many requests should use :class:`_DatabricksTokenAuth`,
    which reuses the SDK config across requests.

    :param server_url: The remote server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: A bearer token, or ``None`` when no pointer record is
        stored or the workspace credentials don't resolve.
    """
    from omnigent.cli_auth import load_databricks_workspace_host
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    workspace_host = load_databricks_workspace_host(server_url)
    if workspace_host is None:
        return None
    try:
        auth, _host = _resolve_databricks_auth(host=workspace_host)
        return auth.current_token()
    except (DatabricksAuthError, ImportError, ValueError):
        return None

def _server_headers(
    *,
    runner_id: str | None = None,
) -> dict[str, str]:
    """
    Build non-auth HTTP headers for an Omnigent server client.

    Auth is handled separately via :func:`_server_auth` which
    returns an ``httpx.Auth`` that refreshes the Databricks OAuth
    token on every request.

    :param runner_id: Optional runner UUID, e.g.
        ``"runner_0123456789abcdef"``. Accepted for callers that
        already threaded the value here; runner affinity is now
        persisted through ``PATCH /v1/sessions/{id}``, not a
        request header.
    :returns: Static headers for ``httpx`` / ``OmnigentClient``.
    """
    del runner_id
    return {}

def _server_auth(
    server_url: str | None = None,
) -> httpx.Auth | None:
    """
    Build an httpx Auth for a remote Omnigent server client.

    Returns a :class:`_DatabricksTokenAuth` when any credential
    source is available (env var, stored ``omnigent login`` record,
    or ambient Databricks credentials). Returns ``None`` for local
    servers that don't need auth, so the caller can pass it straight
    to ``OmnigentClient(auth=...)``.

    :param server_url: Optional remote server URL for looking up
        stored OIDC tokens.
    :returns: Auth instance, or ``None``.
    """
    raw = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
    if raw and raw.strip():
        return _DatabricksTokenAuth(server_url=server_url)
    # Runner-tunnel identity (BDP-2437): a host/daemon-launched runner has only
    # its tunnel binding token (no user bearer). Return the interceptor so it
    # injects ``X-Omnigent-Runner-Tunnel-Token`` on every request — otherwise
    # the forwarder runs with ``auth=None`` and every runner->server call 401s.
    from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR

    tunnel = os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR)
    if tunnel and tunnel.strip():
        return _DatabricksTokenAuth(server_url=server_url)
    # Check stored `omnigent login` records: a session JWT or a
    # Databricks Apps pointer record.
    if server_url:
        from omnigent.cli_auth import load_databricks_workspace_host, load_token

        if load_token(server_url) or load_databricks_workspace_host(server_url):
            return _DatabricksTokenAuth(server_url=server_url)
    creds = _read_databrickscfg(None)
    if creds is not None and creds.token:
        return _DatabricksTokenAuth(server_url=server_url)
    return None

def _chat_with_server(
    server_url: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    agent_name: str | None = None,
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    log: bool = False,
    agent_yaml: Path | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    ephemeral: bool = False,
    debug_events: bool = False,
    server_log_path: Path | None = None,
    runner_log_path: Path | None = None,
    resume_parts: list[str] | None = None,
    skills: list[SkillSpec] | None = None,
    auto_open_conversation: bool = False,
    progress: RunnerStartupProgress | None = None,
    attach_only: bool = False,
    attach_harness: str | None = None,
) -> None:
    """
    Connect to a server URL and run a one-shot query or REPL.

    Lists available agents and lets the user pick one unless
    *agent_name* is supplied by an upstream setup step such as
    ephemeral ``--server`` upload.

    :param server_url: The server URL.
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set without
        ``resume_conversation_id``, run one request and exit. If set
        with ``resume_conversation_id``, auto-send when the REPL
        opens.
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation on the remote
        server instead of creating a fresh one.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The REPL opens attached to the fork.
    :param agent_name: Optional already-selected agent name,
        e.g. ``"hello_world"``.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that restarts a local
        runner if it exits and returns the live runner id.
    :param log: When ``True``, write a session log on REPL exit.
    :param agent_yaml: Optional local agent YAML path for tmux
        pane re-launch metadata.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh ``/v1/sessions`` session. Required for
        fresh sessions on the sessions API path.
    :param session_bundle_filename: Filename for the multipart
        ``bundle`` part, e.g. ``"agent.tar.gz"``.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that won't
        survive process exit.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline. Forwarded to ``_run_repl``.
    :param server_log_path: Path to the local server's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/server/server-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` for remote servers.
    :param runner_log_path: Path to the local runner's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/runner/runner-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` when no local runner is used.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnigent", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` uses the current process argv.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command in the REPL.
        ``None`` (default) means no skill commands are registered.
    :param progress: Active startup spinner handed off from the daemon
        bring-up path, or ``None``. It stays up (on its last label,
        ``"Launching your agent…"``) across the wrapper-redirect probe and
        REPL setup below — so there's no empty gap there — and is cleared
        (``progress.finish()``) the instant before this function produces
        terminal output — a native-wrapper redirect notice, the one-shot
        reply, or the REPL's first paint.
    """
    base_url = server_url.rstrip("/")

    # The spinner (still showing the last bring-up phase, "Launching your
    # agent…") is intentionally left running through the wrapper-redirect
    # probe (a ``GET /v1/sessions/{id}`` that can take a few seconds) and REPL
    # setup, so the user never sees a cleared spinner over an empty gap here.
    # The label lags the exact step on purpose — better than a vaguer one.

    # Wrapper-aware resume redirect: if the conversation we're about to
    # resume was originally created by a terminal-native wrapper, the AP
    # REPL is the WRONG surface to attach to. Detect via the
    # ``omnigent.wrapper`` label on the conversation and re-dispatch
    # into the native wrapper carrying ``--server`` through. Without
    # this, the REPL renders an empty chat on top of a
    # session whose state lives in a tmux terminal it can't see.
    if resume_conversation_id is not None and _redirect_native_resume_if_needed(
        base_url=base_url,
        conversation_id=resume_conversation_id,
        auto_open_conversation=auto_open_conversation,
        progress=progress,
    ):
        return

    selected_agent = agent_name or _pick_agent(base_url)

    # Bring-up is done — clear the spinner the instant before we produce
    # terminal output (the one-shot reply or the REPL's first paint), so it
    # never lingers across the hand-off but also never leaves a gap before it.
    if progress is not None:
        progress.finish()

    if initial_message is not None:
        _run_one_shot(
            base_url=base_url,
            agent_name=selected_agent,
            tool_handler=tool_handler,
            prompt=initial_message,
            runner_id=runner_id,
            session_bundle=session_bundle,
            session_bundle_filename=session_bundle_filename,
            resume_conversation_id=resume_conversation_id,
            auto_open_conversation=auto_open_conversation,
        )
        return

    _run_repl(
        base_url,
        selected_agent,
        tool_handler,
        initial_message=initial_message,
        resume_conversation_id=resume_conversation_id,
        fork_session_id=fork_session_id,
        log=log,
        agent_yaml=agent_yaml,
        runner_id=runner_id,
        runner_recover=runner_recover,
        session_bundle=session_bundle,
        session_bundle_filename=session_bundle_filename,
        ephemeral=ephemeral,
        debug_events=debug_events,
        server_log_path=server_log_path,
        runner_log_path=runner_log_path,
        resume_parts=resume_parts,
        skills=skills,
        auto_open_conversation=auto_open_conversation,
        attach_only=attach_only,
        attach_harness=attach_harness,
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
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
    for _key, _value in _sib_native.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overrides.__dict__.items():
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
