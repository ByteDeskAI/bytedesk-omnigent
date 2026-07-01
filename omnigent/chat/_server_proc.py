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

def _find_free_port() -> int:
    """
    Find a free TCP port.

    :returns: An available port number.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])

def _omnigent_log_dir() -> Path:
    """
    Resolve the shared Omnigent process log directory.

    Server and captured runner stdout/stderr logs live under the
    same per-user state root as session transcripts and CLI
    diagnostics, rather than under the system temp directory.

    :returns: ``~/.omnigent/logs``, created if needed.
    """
    log_dir = Path.home() / ".omnigent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

def _omnigent_persistent_dir() -> Path:
    """
    Resolve the persistent omnigent data directory.

    Honors ``OMNIGENT_DATA_DIR`` (the data-isolation knob a worktree sets
    to avoid sharing ``~/.omnigent/chat.db``), else lives at
    ``~/.omnigent`` alongside the native paths ``sessions/`` and ``logs/``
    (see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md). Created on first access;
    subsequent calls are idempotent.

    Must resolve identically to
    :func:`omnigent.host.local_server._local_data_dir` — the local server
    writes its DB under that dir while ``omnigent run`` reads the resume DB
    from here, so a divergence would silently lose history. ``OMNIGENT_CONFIG_HOME``
    is intentionally not consulted (it isolates config, not data).

    :returns: The absolute path to the persistent dir,
        guaranteed to exist along with the ``artifacts/``
        subdir.
    """
    override = os.environ.get("OMNIGENT_DATA_DIR")
    ap_dir = Path(override).expanduser() if override else Path.home() / ".omnigent"
    ap_dir.mkdir(parents=True, exist_ok=True)
    (ap_dir / "artifacts").mkdir(exist_ok=True)
    return ap_dir

def _start_local_server(
    agent_path: Path,
    port: int,
    *,
    ephemeral: bool = False,
) -> LocalServer:
    """
    Launch a local Omnigent server.

    Server stdout/stderr are routed to ``server.log`` in a
    per-run directory under ``~/.omnigent/logs`` so concurrent Omnigent sessions don't
    interleave. The log path is returned to the caller (via
    :class:`LocalServer`) so :func:`_raise_server_failed`
    can surface it in its error message — critical because
    the REPL only surfaces the wrapped ``PermanentLLMError``
    string, not the underlying cause (e.g. Codex App Server
    403s, missing binaries, credential resolution mismatches).

    The data store (SQLite DB + artifacts) lives at
    ``~/.omnigent/{chat.db,artifacts/}`` by default —
    persistent across runs so ``--continue`` / ``--resume``
    can resume prior conversations
    (designs/RUN_OMNIGENT_SESSION_RESUMPTION.md). Pass
    ``ephemeral=True`` to opt back into a fresh per-run
    tmpdir (the ``--no-session`` shape).

    :param agent_path: Path to the agent directory,
        e.g. ``Path("examples/archer")``.
    :param port: Port the server will listen on, e.g. ``8900``.
    :param ephemeral: When ``True``, place the SQLite DB and
        artifacts in a fresh tmpdir instead of the persistent
        ``~/.omnigent`` location. Used for ``--no-session``
        runs and for tests that want isolation between
        invocations.
    :returns: The server handle bundling the subprocess and
        the path to its captured stdout/stderr log file.
    """
    log_dir = _omnigent_log_dir() / "server"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fd, log_name = tempfile.mkstemp(prefix="server-", suffix=".log", dir=log_dir)
    log_path = Path(log_name)
    log_fh = os.fdopen(log_fd, "wb")
    if ephemeral:
        data_tmpdir = tempfile.mkdtemp(prefix="ap-chat-data-")
        db_path = Path(data_tmpdir) / "chat.db"
        artifact_path = Path(data_tmpdir) / "artifacts"
    else:
        data_dir = _omnigent_persistent_dir()
        db_path = data_dir / "chat.db"
        artifact_path = data_dir / "artifacts"

    # Plain file for stdout/stderr — not subprocess.PIPE. PIPE would
    # deadlock once the kernel's ~64 KB pipe buffer fills (e.g. under
    # the Codex MCP notification firehose) because nothing drains it;
    # a file has no such limit. The child dup's the fd at Popen time,
    # so we close our parent-side handle immediately after spawn —
    # explicit close beats GC ordering for fd lifetime management.
    from omnigent.cli import _start_cli_runner_process
    from omnigent.runner.identity import token_bound_runner_id

    # Generate a binding token and derive the runner_id from it.
    # Both the server and runner receive the token so the server's
    # tunnel route accepts exactly this runner's WebSocket upgrade.
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # Build the server's child environment. The tunnel token lets the
    # server restrict its runner-tunnel allowlist to the sibling runner
    # we spawn below (read by server() via OMNIGENT_RUNNER_TUNNEL_TOKEN).
    #
    # Accounts opt-in: when the parent shell selects accounts mode
    # (OMNIGENT_AUTH_PROVIDER=accounts), inject the per-spawn
    # base URL + cookie secret so the spawned server's
    # AccountsConfig.from_env() can satisfy its required-fields check.
    # Mirrors the same logic in cli.py:_ensure_local_omnigent_server; see
    # the comment block there for the full UX explanation. (Two
    # spawn paths exist because `omnigent run --server ""` goes
    # through _ensure_local_omnigent_server while `omnigent run` with
    # an agent spec and no --server flag goes through here.)
    child_env = {
        **os.environ,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        # Single-user loopback runtime — see ensure_local_omnigent_server for why
        # this lets the host tunnel re-own this machine's host_id across an
        # auth-mode flip without weakening the deployed multi-user boundary.
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    # Mirror create_auth_provider's resolution so this spawn path agrees
    # with the daemon path (host/local_server.py::ensure_local_omnigent_server):
    # header is the env-unset default; OMNIGENT_AUTH_ENABLED=1 opts into
    # accounts (or oidc when OMNIGENT_OIDC_* is set). In header/oidc mode we
    # must NOT mint an accounts cookie secret (those modes never read it).
    from omnigent.server.auth import resolve_auth_source

    _accounts_mode = resolve_auth_source() == "accounts"
    if _accounts_mode:
        if "OMNIGENT_ACCOUNTS_COOKIE_SECRET" not in os.environ:
            child_env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = secrets.token_hex(32)
        # Always override BASE_URL — the parent's value (if any) almost
        # certainly points at a different port than the freshly picked
        # one. Surprises here ("why is my magic URL wrong?") are worse
        # than discarding an out-of-date setting.
        child_env["OMNIGENT_ACCOUNTS_BASE_URL"] = f"http://127.0.0.1:{port}"
    # Propagate executor.profile from the spec as DATABRICKS_CONFIG_PROFILE
    # (spec self-containment: the YAML's own declaration is the only thing
    # that selects a Databricks workspace here — there is no CLI override).
    # This ensures the Omnigent server and its runner subprocess resolve credentials
    # for the right Databricks workspace (LLM calls, compaction, etc.).
    if "DATABRICKS_CONFIG_PROFILE" not in child_env:
        _spec = load_spec(agent_path)
        if _spec.executor.profile:
            child_env["DATABRICKS_CONFIG_PROFILE"] = _spec.executor.profile

    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_path),
                "--agent",
                str(agent_path),
            ],
            env=child_env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        log_fh.close()

    # Spawn the runner as a sibling subprocess (not a child of the
    # server). The runner retries its WS tunnel connection until the
    # server is ready, so launching them concurrently is safe.
    # If the runner fails to start, kill the already-running server
    # so the caller's finally block doesn't orphan it.
    _prewarm_spec = agent_path if agent_path.exists() else None
    try:
        runner = _start_cli_runner_process(
            server_url=f"http://127.0.0.1:{port}",
            tunnel_token=binding_token,
            runner_id=runner_id,
            workspace_cwd=Path.cwd(),
            prewarm_spec_path=_prewarm_spec,
            isolate_session=True,
            # Route runner stdio to a log file; otherwise its INFO logs
            # paint onto the parent REPL / one-shot stderr.
            capture_logs=True,
        )
    except BaseException:
        _stop_server(server_proc)
        raise

    return LocalServer(
        proc=server_proc,
        log_path=log_path,
        runner_id=runner_id,
        runner_proc=runner.proc,
    )

def _wait_for_server(port: int, server: LocalServer, timeout: float = 45.0) -> None:
    """
    Poll until the server responds.

    :param port: The server port.
    :param server: The launched server handle. Used to detect
        early exit (via ``server.proc.poll()``) and to surface
        ``server.log_path`` in the failure message.
    :param timeout: Max seconds to wait.
    :raises click.ClickException: If the server doesn't start.
    """
    base_url = f"http://127.0.0.1:{port}"
    start = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            _raise_server_failed(server)
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                runner_id = server.runner_id
                if runner_id is None:
                    return
                runner_resp = httpx.get(
                    f"{base_url}/v1/runners/{runner_id}/status",
                    timeout=2.0,
                )
                if runner_resp.status_code == 200 and runner_resp.json()["online"] is True:
                    return
        except httpx.ConnectError:
            pass
        elapsed = time.monotonic() - start
        poll_interval = (
            _SERVER_READY_INITIAL_POLL_SECONDS
            if elapsed < _SERVER_READY_FAST_POLL_WINDOW_SECONDS
            else _SERVER_READY_BACKOFF_POLL_SECONDS
        )
        time.sleep(poll_interval)
    _raise_server_failed(server)

def _raise_server_failed(server: LocalServer) -> None:
    """
    Raise a descriptive error for a failed server startup.

    Includes the tail of the server log inline so CI failures (where
    the user can't tail the file by hand) carry the underlying
    traceback in the test's stderr. The path is still printed for
    local runs where the user may want the full file.

    :param server: The launched server handle, used to recover
        the subprocess command line and log file location.
    :raises click.ClickException: Always.
    """
    args = server.proc.args
    if isinstance(args, list):
        parts = [p.decode() if isinstance(p, bytes) else str(p) for p in args]
        cmd_display = " ".join(parts)
    elif isinstance(args, bytes):
        cmd_display = args.decode()
    else:
        cmd_display = str(args)
    try:
        lines = server.log_path.read_text(errors="replace").splitlines()
        tail = "\n".join(lines[-_SERVER_LOG_TAIL_LINES:]) if lines else "(empty log file)"
    except OSError as e:
        tail = f"(could not read log file: {e})"
    raise click.ClickException(
        "Server failed to start.\n"
        f"  Server log:  {server.log_path}\n"
        "  Re-run the server directly to see its output:\n"
        f"    {cmd_display}\n"
        f"\n  Last {_SERVER_LOG_TAIL_LINES} lines of {server.log_path}:\n"
        f"{tail}"
    )

def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    """
    Gracefully stop the server subprocess.

    :param proc: The server subprocess.
    """
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)

def _stop_local_server(server: LocalServer) -> None:
    """
    Stop both the server and its sibling runner subprocess.

    :param server: The :class:`LocalServer` handle returned by
        :func:`_start_local_server`.
    """
    from omnigent.cli import _stop_cli_runner_process

    if server.runner_proc is not None:
        try:
            _stop_cli_runner_process(server.runner_proc)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            logger.warning(
                "Failed to stop local runner cleanly",
                exc_info=True,
            )
    _stop_server(server.proc)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
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
    for _key, _value in _sib_remote.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_repl.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_sessions.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
