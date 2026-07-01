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
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _runner_loopback_host(host: str) -> str:
    """Return a loopback-safe host for local runner callbacks.

    :param host: Server bind host, e.g. ``"0.0.0.0"``.
    :returns: Hostname the local runner can call back, e.g.
        ``"127.0.0.1"``.
    """
    return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host

class _CliRunnerProcess:
    """Runner subprocess metadata for the ``omnigent server`` command.

    :param proc: Runner subprocess handle.
    :param runner_id: Runner id used for the WS tunnel, e.g.
        ``"runner_0123456789abcdef"``.
    :param tunnel_token: Secret token that binds the tunnel to
        ``runner_id``, e.g. ``"uA6Zz..."``.
    """

    proc: subprocess.Popen[bytes]
    runner_id: str
    tunnel_token: str
    log_path: Path | None = None

def _start_cli_runner_process(
    *,
    server_url: str,
    tunnel_token: str | None = None,
    runner_id: str | None = None,
    workspace_cwd: str | Path | None = None,
    capture_logs: bool = False,
    log_dir: str | Path | None = None,
    prewarm_spec_path: str | Path | None = None,
    isolate_session: bool = False,
) -> _CliRunnerProcess:
    """Start the out-of-process runner used by CLI server flows.

    The runner always connects back over the WebSocket tunnel. Local
    ``omnigent server`` passes its loopback URL; ``run --server``
    passes the remote Omnigent server URL.

    For remote Databricks-fronted servers, the runner subprocess
    authenticates via the stored ``omnigent login`` record (or
    ambient Databricks SDK credentials). Tokens are refreshed
    transparently on each WebSocket reconnect and HTTP callback —
    no static token is passed via environment variable.

    :param server_url: Server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param tunnel_token: Optional binding token for the runner id,
        e.g. ``"uA6Zz..."``. ``None`` generates a fresh token.
    :param runner_id: Optional runner id to advertise. ``None``
        uses a per-run token-bound id for authenticated remote
        servers, or the stable runner id from
        :func:`omnigent.runner.identity.get_stable_runner_id`
        for unauthenticated local servers.
    :param workspace_cwd: Optional local workspace root to expose
        to runner-local filesystem tools when a spec uses the
        placeholder cwd ``"."``. Remote ``run/attach --server``
        passes the CLI launch cwd so local runner tools operate
        in the user's project checkout.
    :param capture_logs: When True, redirect the runner
        subprocess's stdout/stderr to a per-run temp log file
        instead of inheriting the parent's stdio. The attach-remote
        flow sets this so runner WARNINGs (e.g. expected
        tunnel-dispatch failures like sandbox-unsupported)
        don't paint onto the REPL terminal.
    :param log_dir: Optional base log directory to use when
        ``capture_logs`` is true. Defaults to the shared
        ``~/.omnigent/logs`` location; tests should pass a
        temporary directory to avoid writing to the developer's
        real home.
    :param prewarm_spec_path: Optional YAML path; the runner spawns
        its MCPs during the upload window. See designs/RUNNER_MCP.md.
    :param isolate_session: ``True`` for shared-host runners;
        enables per-session workspace isolation so each
        session gets its own subdirectory. ``False`` (default)
        lets the agent see the project root directly.
    :returns: The spawned runner process metadata.
    :raises click.ClickException: If the runner exits immediately.
    """
    from omnigent.runner.identity import (
        RUNNER_ID_ENV_VAR,
        RUNNER_ISOLATE_SESSION_ENV_VAR,
        RUNNER_PARENT_PID_ENV_VAR,
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
        RUNNER_WORKSPACE_ENV_VAR,
        token_bound_runner_id,
    )

    binding_token = tunnel_token.strip() if tunnel_token is not None else None
    if tunnel_token is not None and not binding_token:
        raise click.ClickException("Runner tunnel binding token must not be empty")
    binding_token = binding_token or secrets.token_urlsafe(32)
    resolved_runner_id = runner_id.strip() if runner_id is not None else None
    if runner_id is not None and not resolved_runner_id:
        raise click.ClickException("Runner id must not be empty")
    if resolved_runner_id is None:
        # The runner sends the binding token in the tunnel header;
        # the server derives expected_runner_id from it via
        # token_bound_runner_id(). The path runner_id must match,
        # so we always derive from the binding token — not the
        # stable runner id, which is unrelated to the token.
        resolved_runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "RUNNER_SERVER_URL": server_url,
        RUNNER_ID_ENV_VAR: resolved_runner_id,
        RUNNER_PARENT_PID_ENV_VAR: str(os.getpid()),
    }
    env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] = binding_token
    if workspace_cwd is not None:
        env[RUNNER_WORKSPACE_ENV_VAR] = str(Path(workspace_cwd).expanduser().resolve())
    if isolate_session:
        env[RUNNER_ISOLATE_SESSION_ENV_VAR] = "1"
    if prewarm_spec_path is not None:
        env["RUNNER_PREWARM_SPEC_PATH"] = str(Path(prewarm_spec_path).expanduser().resolve())

    log_path: Path | None = None
    log_fh: BinaryIO | None = None
    if capture_logs:
        base_log_dir = (
            Path(log_dir).expanduser()
            if log_dir is not None
            else Path.home() / ".omnigent" / "logs"
        )
        runner_log_dir = base_log_dir / "runner"
        runner_log_dir.mkdir(parents=True, exist_ok=True)
        log_fd, log_name = tempfile.mkstemp(prefix="runner-", suffix=".log", dir=runner_log_dir)
        log_path = Path(log_name)
        log_fh = os.fdopen(log_fd, "wb")
    try:
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        if log_fh is not None:
            log_fh.close()
    if runner_proc.poll() is not None:
        from omnigent._runner_startup import format_runner_log_tail

        raise click.ClickException(
            f"Runner process exited early with code {runner_proc.returncode}."
            f"{format_runner_log_tail(log_path)}"
        )
    return _CliRunnerProcess(
        proc=runner_proc,
        runner_id=resolved_runner_id,
        tunnel_token=binding_token,
        log_path=log_path,
    )

def _stop_cli_runner_process(
    proc: subprocess.Popen[bytes],
    *,
    grace_timeout: float = 5.0,
) -> None:
    """Stop a runner subprocess started by :func:`_start_cli_runner_process`.

    :param proc: Runner subprocess handle to terminate.
    :param grace_timeout: Seconds to wait after SIGTERM before
        sending SIGKILL, e.g. ``5.0``.
    :returns: None.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=grace_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

def _adopt_cli_runner_process(proc: subprocess.Popen[bytes]) -> None:
    """Detach a runner from this CLI so it keeps running after CLI exit.

    Sends :data:`RUNNER_ADOPT_SIGNAL` (SIGUSR1) so the runner cancels
    its parent-pid watchdog and survives the launching CLI's exit. Used
    when the user detaches from tmux: Claude and the runner stay alive
    and the web UI stays connected. A no-op if the runner
    has already exited.

    :param proc: Runner subprocess handle to adopt.
    :returns: None.
    """
    from omnigent.runner.identity import RUNNER_ADOPT_SIGNAL

    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(RUNNER_ADOPT_SIGNAL)

