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

def _await_accounts_first_run_setup(
    base_url: str,
    *,
    timeout_s: float = _ACCOUNTS_SETUP_TIMEOUT_S,
    progress: RunnerStartupProgress | None = None,
) -> None:
    """Block until a fresh accounts-mode local server has its first admin.

    When ``omnigent run`` (re)spawns the local Omnigent server in accounts mode on
    a machine with no admin yet, the server reports ``needs_setup`` and (by
    default) opens a browser to its Create-admin form. Until an admin is
    claimed there is no CLI credential, so the first authenticated call would
    401. Rather than crash, print the setup URL — so it works whether or not
    the browser auto-opened (e.g. ``OMNIGENT_ACCOUNTS_AUTO_OPEN=0``) — and
    poll until the admin is created; ``/auth/setup`` then mints this CLI's
    loopback token, which we detect and return on.

    No-op when the server is not in accounts mode, when this CLI already holds
    a token for *base_url*, or when an admin already exists (the server mints
    our token at boot in that case).

    :param base_url: Resolved local Omnigent server URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param timeout_s: Max seconds to wait for setup, e.g. ``600.0``.
    :param progress: Active startup spinner, if any. Cleared before the
        interactive setup prompt below so the spinner doesn't animate over
        it. ``None`` (the default) when no spinner is running.
    :raises click.ClickException: If setup does not complete in time.
    """
    from omnigent import cli_auth

    # Already authenticated to this server — nothing to wait for.
    if cli_auth.load_token(base_url) is not None:
        return
    try:
        info = httpx.get(f"{base_url}/v1/info", timeout=5.0).json()
    except (httpx.HTTPError, ValueError):
        # /v1/info unreachable / unparseable: don't block — let the normal
        # path run and surface any real error.
        return
    if not (isinstance(info, dict) and info.get("accounts_enabled") and info.get("needs_setup")):
        # Header / OIDC, or an admin already exists (token minted at boot):
        # the normal headers/auth path handles it.
        return

    # We're about to print an interactive prompt and poll — drop the startup
    # spinner first so it doesn't render over the message.
    if progress is not None:
        progress.finish()
    setup_url = base_url.rstrip("/")
    click.echo(
        "\n  Accounts mode is enabled and needs a one-time admin account.\n"
        f"  Open  {setup_url}  in your browser to create it"
        " (it may have opened automatically),\n"
        "  then come back here. Waiting for setup to complete… (Ctrl-C to cancel)\n"
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(_ACCOUNTS_SETUP_POLL_INTERVAL_S)
        if cli_auth.load_token(base_url) is not None:
            click.echo("  ✓ Admin created — signed in. Continuing.\n")
            return
    raise click.ClickException(
        f"Timed out after {timeout_s:.0f}s waiting for admin setup at {setup_url}. "
        "Create the admin in the browser, then re-run."
    )

async def _prepare_chat_session_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    auth: httpx.Auth | None,
    host_id: str,
    bundle: bytes,
    resume_conversation_id: str | None,
    fork_session_id: str | None,
    workspace: str,
    progress: RunnerStartupProgress | None = None,
) -> _DaemonChatSession:
    """
    Create/resolve a chat session and launch a daemon-owned runner for it.

    Resolves the target session — fork > resume > fresh create — then asks
    the daemon to spawn a runner bound to it (the daemon owns the runner;
    the CLI only attaches the REPL afterward). Mirrors claude-native's
    ``_prepare_claude_terminal_via_daemon`` minus the terminal bring-up.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:8123"``.
    :param headers: Static HTTP auth headers (empty for a loopback server).
    :param auth: Per-request ``httpx.Auth`` for token refresh on the SDK
        client, or ``None`` for a loopback server.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param bundle: Gzipped agent bundle for a fresh session create.
    :param resume_conversation_id: Existing conversation id to attach to,
        or ``None`` to create a fresh session.
    :param fork_session_id: When set, fork this session and bind the runner
        to the fork; takes precedence over *resume_conversation_id*.
    :param workspace: Absolute host path for the runner cwd, e.g.
        ``"/Users/me/proj"``.
    :param progress: Optional startup-progress handle whose label is
        advanced through plain-language phases ("Connecting…",
        "Launching your agent…") as the host and runner come online, so a
        slow cold start is not silent. ``None`` (the default) runs without
        any progress updates.
    :returns: The prepared session id + bound runner id.
    :raises click.ClickException: If session create/fork or runner launch
        fails.
    """
    from omnigent_client import OmnigentClient

    from omnigent._runner_startup import (
        STARTUP_PHASE_CONNECTING,
        STARTUP_PHASE_LAUNCHING_AGENT,
    )
    from omnigent.host.daemon_launch import (
        launch_or_reuse_daemon_runner,
        wait_for_host_online,
        wait_for_runner_online,
    )
    from omnigent.native_terminal import bind_session_runner

    async with OmnigentClient(base_url=base_url, headers=headers, auth=auth) as sdk:
        if fork_session_id is not None:
            fork_result = await sdk.sessions.fork(fork_session_id)
            session_id = fork_result["id"]
        elif resume_conversation_id is not None:
            session_id = resume_conversation_id
        else:
            created = await sdk.sessions.create(
                bundle, filename="agent.tar.gz", workspace=workspace
            )
            session_id = created.id

    # A separate raw httpx client for the host-runner protocol (the daemon
    # launch helpers operate on httpx, not the SDK).
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        if progress is not None:
            progress.update(STARTUP_PHASE_CONNECTING)
        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_CHAT_HOST_ONLINE_TIMEOUT_S)
        if progress is not None:
            progress.update(STARTUP_PHASE_LAUNCHING_AGENT)
        runner_id = await launch_or_reuse_daemon_runner(
            client, host_id=host_id, session_id=session_id, workspace=workspace
        )
        await wait_for_runner_online(
            client, runner_id, timeout_s=_DAEMON_CHAT_RUNNER_ONLINE_TIMEOUT_S
        )
        # launch_or_reuse_daemon_runner's atomic-bind / online-reuse paths
        # don't pass through replace_runner_id, so re-bind via PATCH to
        # clear the ``omnigent.stopped`` marker on resumed sessions. Must run
        # AFTER wait_for_runner_online — a freshly launched runner isn't
        # registered until then, and replace_runner_id 400s on an unregistered id.
        await bind_session_runner(client, session_id, runner_id)
    return _DaemonChatSession(session_id=session_id, runner_id=runner_id)

def _chat_via_daemon(
    agent_path: str,
    base_url: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    resume_latest: bool = False,
    resume_picker: bool = False,
    fork_session_id: str | None = None,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Run a local agent against a daemon-backed server with a daemon-owned runner.

    Uploads the agent as a session, asks the host daemon to spawn the
    runner bound to that session (so the daemon owns its lifecycle), then
    attaches the REPL — the CLI never spawns or tears down the runner. On a
    clean exit the server idle-reaps the runner; if it dies mid-session the
    server relaunches it (host-bound auto-relaunch).

    :param agent_path: Local YAML path or directory.
    :param base_url: Resolved Omnigent server base URL (the daemon is already
        ensured for it), e.g. ``"http://127.0.0.1:8123"``.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the uploaded spec.
    :param initial_message: Optional one-shot input (``-p``).
    :param resume_conversation_id: Explicit conversation id
        (``--resume <id>``).
    :param resume_latest: ``True`` for ``--continue`` / ``-c``.
    :param resume_picker: ``True`` for bare ``--resume`` / ``-r``.
    :param fork_session_id: When set, fork this session and attach to the fork.
    :param log: When ``True``, write a session log on REPL exit.
    :param debug_events: When ``True``, enable the SSE-to-UI debug pipeline.
    :param resume_parts: Argument-list prefix for the resume hint on exit.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL once the session id is known.
    :returns: None.
    """
    from omnigent.host.identity import load_or_create_host_identity

    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    from omnigent._runner_startup import (
        STARTUP_PHASE_PREPARING_AGENT,
        runner_startup_progress,
    )

    spec_path = _materialize_override_bundle(path, overrides)
    try:
        # One spinner spans the entire agent/runner bring-up — bundle prep,
        # session upload, host + runner coming online, and the
        # wrapper-redirect probe inside ``_chat_with_server`` — and is torn
        # down (via ``progress.finish()``) exactly when the REPL is about to
        # paint. Holding a single spinner across these steps means there is
        # never an empty, cleared gap between them; the label can lag the
        # actual step (it stays on the last phase through the tail of
        # bring-up), which is the accepted trade for "no blank gaps". The
        # local-server cold start before this is covered by a sibling spinner
        # in ``_ensure_backend``.
        with runner_startup_progress(initial_message=STARTUP_PHASE_PREPARING_AGENT) as progress:
            _validate_agent_spec(spec_path)

            agent_spec = load_spec(spec_path)
            agent_name = agent_spec.name or _fallback_label(spec_path)
            all_skills = _merge_host_skills(agent_spec, spec_path)
            bundle_bytes = _bundle_agent(spec_path)

            # Accounts first-run: if the (re)spawned local server is in
            # accounts mode awaiting its one-time admin and this CLI has no
            # credential yet, block here with a clear prompt until setup
            # completes (the server then mints our CLI token), so we continue
            # into the REPL instead of 401-ing on the first authenticated call
            # below. Resolved BEFORE the headers/auth below so they pick up the
            # freshly-written token. ``progress`` is threaded so it can clear
            # the spinner before printing its interactive prompt.
            _await_accounts_first_run_setup(base_url, progress=progress)

            headers = _remote_headers(server_url=base_url)
            auth = _server_auth(server_url=base_url)
            host_id = load_or_create_host_identity().host_id
            workspace = str(Path.cwd().resolve())

            # The interactive resume picker reads stdin, so clear the spinner
            # first — it must not animate over the prompt. ``--continue`` and an
            # explicit ``--resume <id>`` are silent lookups and keep the spinner.
            if resume_picker:
                progress.finish()
            # Resolve --continue / --resume / picker to a concrete conversation
            # id. Fork is resolved server-side inside the prep step, so skip it.
            effective_resume_id = (
                None
                if fork_session_id is not None
                else _resolve_resume_target(
                    base_url=base_url,
                    agent_name=agent_name,
                    resume_conversation_id=resume_conversation_id,
                    resume_latest=resume_latest,
                    resume_picker=resume_picker,
                    headers=headers,
                )
            )

            prepared = asyncio.run(
                _prepare_chat_session_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    auth=auth,
                    host_id=host_id,
                    bundle=bundle_bytes,
                    resume_conversation_id=effective_resume_id,
                    fork_session_id=fork_session_id,
                    workspace=workspace,
                    progress=progress,
                )
            )

            # Attach the REPL to the prepared session. ``resume_conversation_id``
            # makes the sessions adapter attach (get) instead of creating from
            # the bundle; the bundle is still passed so the one-shot path takes
            # its sessions-API branch. ``runner_recover=None``: the daemon owns
            # the runner — a dead runner is relaunched server-side, and the SDK
            # client refreshes its own auth per request. ``progress`` is handed
            # off so ``_chat_with_server`` clears the spinner the instant before
            # the REPL paints (or before it redirects to a native wrapper).
            _chat_with_server(
                base_url,
                tool_handler,
                initial_message=initial_message,
                resume_conversation_id=prepared.session_id,
                fork_session_id=None,
                agent_name=agent_name,
                runner_id=prepared.runner_id,
                runner_recover=None,
                log=log,
                agent_yaml=spec_path,
                session_bundle=bundle_bytes,
                debug_events=debug_events,
                resume_parts=resume_parts,
                skills=all_skills or None,
                auto_open_conversation=auto_open_conversation,
                progress=progress,
            )
    finally:
        _cleanup_materialized_override_bundle(spec_path)

def _wait_for_remote_runner(
    base_url: str,
    runner_id: str,
    headers: dict[str, str],
    runner_proc: subprocess.Popen[bytes],
    timeout: float = 60.0,
    *,
    log_path: Path | None = None,
    show_progress: bool = True,
) -> None:
    """Wait until the remote server sees the local runner tunnel.

    :param base_url: Remote server base URL with no trailing slash,
        e.g. ``"https://example.databricksapps.com"``.
    :param runner_id: Runner id the local process advertises, e.g.
        ``"runner_0123456789abcdef"``.
    :param headers: Auth headers for the remote server.
    :param runner_proc: Spawned local runner subprocess.
    :param timeout: Max seconds to wait for registration.
    :param log_path: Optional path to the captured runner log
        produced by ``_start_cli_runner_process(capture_logs=True)``,
        e.g. ``Path("~/.omnigent/logs/runner/runner-abcd.log")``.
        Included (with a tail) in the error message when the
        runner fails to register so users can diagnose the root
        cause without hunting for the file.
    :param show_progress: When ``True`` (default), render a rich
        spinner on stderr while polling. Set to ``False`` for
        callers running after the terminal has entered raw mode
        (e.g. the ``claude-native`` reconnect path) where a
        rich-rendered line would corrupt the attached PTY.
        Auto-falls back to plain ``click.echo`` updates on a
        non-TTY stderr; see :mod:`omnigent._runner_startup`.
    :returns: None.
    :raises click.ClickException: If the runner exits early or the
        server does not report it online before timeout. The
        exception message includes the runner log path and a
        ~20-line tail when ``log_path`` is provided.
    """
    from omnigent._runner_startup import (
        runner_startup_progress,
    )

    host_label = base_url.split("://", 1)[-1]
    initial_msg = f"Starting local runner (waiting for {host_label})\u2026"

    # The two branches return different concrete CMs (rich Status
    # vs nullcontext) but both honor the ``with``-statement
    # protocol. ``Any`` keeps mypy from forcing a structural cast
    # while still allowing the unified branch below.
    progress_cm: contextlib.AbstractContextManager[Any]  # type: ignore[explicit-any]
    if show_progress:
        progress_cm = runner_startup_progress(initial_message=initial_msg)
    else:
        progress_cm = contextlib.nullcontext()

    with progress_cm:
        _poll_remote_runner(
            base_url=base_url,
            runner_id=runner_id,
            headers=headers,
            runner_proc=runner_proc,
            timeout=timeout,
            log_path=log_path,
        )
        return

def _poll_remote_runner(
    *,
    base_url: str,
    runner_id: str,
    headers: dict[str, str],
    runner_proc: subprocess.Popen[bytes],
    timeout: float,
    log_path: Path | None,
) -> None:
    """
    Poll the server's runner-status endpoint until ``online=true``.

    Extracted from :func:`_wait_for_remote_runner` so the polling
    logic is independent of the progress renderer wrapping it.
    Tests that patch ``time.monotonic`` / ``time.sleep`` /
    ``httpx.get`` target this function directly without needing
    to suppress the rich spinner.

    :param base_url: Remote server base URL with no trailing slash.
    :param runner_id: Runner id to poll for.
    :param headers: Auth headers for the remote server.
    :param runner_proc: Spawned local runner subprocess. Used to
        detect early exit so the caller does not poll a dead
        runner for the full timeout.
    :param timeout: Max seconds to wait for registration.
    :param log_path: Captured runner log path threaded into the
        ``ClickException`` raised on failure. ``None`` skips the
        log-tail block in the error message.
    :returns: None on successful registration.
    :raises click.ClickException: Same conditions as
        :func:`_wait_for_remote_runner`.
    """
    from omnigent._runner_startup import format_runner_log_tail

    start = time.monotonic()
    deadline = start + timeout
    status_url = f"{base_url}/v1/runners/{runner_id}/status"
    last_error: httpx.HTTPError | None = None
    last_status: int | None = None
    while time.monotonic() < deadline:
        if runner_proc.poll() is not None:
            raise click.ClickException(
                f"Local runner exited early with code {runner_proc.returncode}."
                f"{format_runner_log_tail(log_path)}"
            )
        try:
            resp = httpx.get(status_url, headers=headers, timeout=2.0)
            if resp.status_code == 200 and resp.json().get("online") is True:
                return
            last_status = resp.status_code
            if resp.status_code in {401, 403}:
                raise click.ClickException(
                    f"Remote runner status check was rejected ({resp.status_code}); "
                    "run `omnigent login <server-url>` or check remote auth credentials."
                    f"{format_runner_log_tail(log_path)}"
                )
        except httpx.HTTPError as exc:
            last_error = exc
        elapsed = time.monotonic() - start
        poll_interval = (
            _SERVER_READY_INITIAL_POLL_SECONDS
            if elapsed < _SERVER_READY_FAST_POLL_WINDOW_SECONDS
            else _SERVER_READY_BACKOFF_POLL_SECONDS
        )
        time.sleep(poll_interval)
    detail = ""
    if last_status is not None:
        detail = f" Last status check returned HTTP {last_status}."
    elif last_error is not None:
        detail = f" Last status check failed: {last_error}."
    raise click.ClickException(
        f"Local runner did not register with {base_url} within {timeout:.0f}s."
        f"{detail}{format_runner_log_tail(log_path)}"
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _sessions as _sib_sessions
    from . import _types as _sib_types
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
