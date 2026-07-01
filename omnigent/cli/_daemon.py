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
from omnigent.cli._config import _effective_global_config_path
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

@dataclass(frozen=True)
class _HostDaemonRecord:
    """
    Local registry record for one background host daemon.

    :param pid: Process id of the background daemon, e.g. ``4242``.
    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :param mode: Launch mode, either ``"server"`` or ``"local"``.
    :param server_url: Normalized requested server URL for ``"server"``
        mode, e.g. ``"https://example.databricksapps.com"``. ``None``
        for local mode.
    :param log_path: Daemon log file path, e.g.
        ``"/Users/me/.omnigent/logs/host-daemon/daemon-abc.log"``.
    :param started_at: Unix epoch seconds when the daemon was spawned,
        e.g. ``1710000000``.
    :param host_id: Local host id advertised to Omnigent servers, e.g.
        ``"host_abc123"``. ``None`` for legacy records.
    :param resolved_server_url: Concrete local server URL discovered for
        local mode, e.g. ``"http://127.0.0.1:8123"``. ``None`` until
        discovery succeeds or for remote mode.
    :param config_sig: Signature of the server-affecting config (resolved
        auth source) the daemon was spawned under, e.g.
        ``"3f9a1c2b4d5e6f70"`` (see :func:`_server_config_signature`).
        ``None`` for legacy records written before config-signature
        tracking existed; a ``None`` signature is never treated as a
        config mismatch (we can't know what it was started with).
    """

    pid: int
    target: str
    mode: str
    server_url: str | None
    log_path: str | None
    started_at: int
    host_id: str | None = None
    resolved_server_url: str | None = None
    config_sig: str | None = None

@dataclass(frozen=True)
class _DaemonSessionsResult:
    """
    Sessions fetched for one daemon target.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``. ``None`` when a
        local daemon's server cannot be discovered.
    :param sessions: Session rows owned by the daemon host id.
    :param error: Human-readable error text, or ``None`` on success.
    """

    base_url: str | None
    sessions: list[_HostSessionRow]
    error: str | None

@dataclass(frozen=True)
class _SpawnedDaemonProcess:
    """
    Background host daemon process metadata.

    :param pid: Spawned process id, e.g. ``4242``.
    :param log_path: Daemon log path, e.g.
        ``"/Users/me/.omnigent/logs/host-daemon/daemon-abc.log"``.
    """

    pid: int
    log_path: str

def _normalize_daemon_target(server_url: str | None) -> str:
    """
    Normalize a daemon target key.

    :param server_url: Requested Omnigent server URL, e.g.
        ``"https://example.databricksapps.com/"``. ``None`` or empty
        string selects local mode.
    :returns: ``"local"`` for local mode, otherwise the URL without a
        trailing slash.
    """
    return _LOCAL_DAEMON_MARKER if not server_url else server_url.rstrip("/")

def _daemon_host_online(record: _HostDaemonRecord, *, timeout_s: float = 2.0) -> bool:
    """
    Probe whether a daemon's host is currently online on its server.

    A daemon process being alive (PID check) does not mean its WebSocket
    tunnel to the Omnigent server is up: the server only reports the host
    ``online`` while a daemon holds an authenticated tunnel and has
    heartbeated within ``HOST_LIVENESS_TTL_S``. After a server restart,
    an ungraceful daemon death, or a flapping tunnel, the daemon can be a
    "zombie" — alive but not registered. This probe distinguishes the two
    so reuse can heal instead of polling a zombie until timeout.

    :param record: Daemon record to probe.
    :param timeout_s: Per-request HTTP timeout in seconds, e.g. ``2.0``.
    :returns: ``True`` only when the server reports the record's host id
        as ``"online"``; ``False`` if the host id is unknown, the server
        is unreachable, or the host reports offline.
    """
    from omnigent.claude_native_bridge import url_component

    host_id = record.host_id or _load_existing_host_id()
    if host_id is None:
        return False
    base_url = _daemon_base_url(record)
    if base_url is None:
        return False
    result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/hosts/{url_component(host_id)}",
        timeout_s=timeout_s,
    )
    if result.status_code != 200 or not isinstance(result.body, dict):
        return False
    return result.body.get("status") == "online"

def _daemon_registry_dir() -> Path:
    """
    Return the directory containing per-target daemon registry records.

    Tests patch :data:`_HOST_PID_PATH`, so derive the registry root from
    the pidfile's parent instead of capturing ``Path.home()`` separately.

    :returns: Registry directory path, e.g.
        ``Path("~/.omnigent/daemons")``.
    """
    return _HOST_PID_PATH.parent / "daemons"

def _daemon_record_path(target: str) -> Path:
    """
    Return the registry JSON path for *target*.

    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :returns: JSON registry path for the target.
    """
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    return _daemon_registry_dir() / f"{digest}.json"

def _record_from_json(raw: _HostJsonObject) -> _HostDaemonRecord | None:
    """
    Parse a daemon record from decoded JSON.

    :param raw: Decoded JSON object, e.g.
        ``{"pid": 4242, "target": "local", "mode": "local"}``.
    :returns: Parsed :class:`_HostDaemonRecord`, or ``None`` if the
        record is malformed.
    """
    try:
        pid_raw = raw["pid"]
        if not isinstance(pid_raw, str | int) or isinstance(pid_raw, bool):
            return None
        pid = int(pid_raw)
        target = str(raw["target"])
        mode = str(raw["mode"])
        started_at_raw = raw["started_at"]
        if not isinstance(started_at_raw, str | int) or isinstance(started_at_raw, bool):
            return None
        started_at = int(started_at_raw)
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in {"local", "server"} or not target:
        return None
    server_url = raw.get("server_url")
    log_path = raw.get("log_path")
    host_id = raw.get("host_id")
    resolved_server_url = raw.get("resolved_server_url")
    config_sig = raw.get("config_sig")
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=server_url if isinstance(server_url, str) and server_url else None,
        log_path=log_path if isinstance(log_path, str) and log_path else None,
        started_at=started_at,
        host_id=host_id if isinstance(host_id, str) and host_id else None,
        resolved_server_url=(
            resolved_server_url
            if isinstance(resolved_server_url, str) and resolved_server_url
            else None
        ),
        config_sig=config_sig if isinstance(config_sig, str) and config_sig else None,
    )

def _read_daemon_record(path: Path) -> _HostDaemonRecord | None:
    """
    Read a daemon registry record from disk.

    :param path: JSON file path to read, e.g.
        ``Path("~/.omnigent/daemons/abc.json")``.
    :returns: Parsed daemon record, or ``None`` if unreadable or malformed.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _record_from_json(cast(_HostJsonObject, raw))

def _write_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Persist a daemon registry record.

    :param record: Record to write, e.g. a local daemon record with
        ``target == "local"``.
    """
    path = _daemon_record_path(record.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")

def _delete_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Delete a daemon registry record if it exists.

    Removes the per-target JSON record, and also clears the legacy
    ``host.pid`` when it names the same target — otherwise a daemon tracked
    only by the legacy pidfile (no JSON record) leaves a phantom that
    reappears on every subsequent ``stop`` / ``host status``.

    :param record: Record whose target path should be removed.
    """
    with contextlib.suppress(OSError):
        _daemon_record_path(record.target).unlink()
    legacy = _read_host_pid_file()
    if legacy is not None and legacy[1] == record.target:
        with contextlib.suppress(OSError):
            _HOST_PID_PATH.unlink()

def _legacy_daemon_record() -> _HostDaemonRecord | None:
    """
    Build a daemon record from the legacy ``host.pid`` file.

    :returns: Legacy record, or ``None`` if the pidfile is absent or
        malformed.
    """
    existing = _read_host_pid_file()
    if existing is None:
        return None
    pid, target = existing
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=0,
        host_id=_load_existing_host_id(),
    )

def _list_daemon_records(*, include_legacy: bool = True) -> list[_HostDaemonRecord]:
    """
    List daemon registry records.

    :param include_legacy: When ``True``, include a synthetic record
        from ``host.pid`` if no JSON record exists for that target.
    :returns: Records ordered by ``started_at`` descending.
    """
    records: dict[str, _HostDaemonRecord] = {}
    registry = _daemon_registry_dir()
    if registry.exists():
        for path in registry.glob("*.json"):
            record = _read_daemon_record(path)
            if record is not None:
                records[record.target] = record
    if include_legacy:
        legacy = _legacy_daemon_record()
        if legacy is not None and legacy.target not in records:
            records[legacy.target] = legacy
    return sorted(records.values(), key=lambda r: r.started_at, reverse=True)

def _find_daemon_record(target: str) -> _HostDaemonRecord | None:
    """
    Find a daemon record by target.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :returns: Matching daemon record, or ``None``.
    """
    for record in _list_daemon_records():
        if record.target == target:
            return record
    return None

def _update_daemon_resolved_server_url(target: str, server_url: str) -> None:
    """
    Record the concrete Omnigent server URL served by a daemon target.

    :param target: Normalized target, e.g. ``"local"``.
    :param server_url: Concrete server URL, e.g.
        ``"http://127.0.0.1:8123"``.
    """
    record = _find_daemon_record(target)
    if record is None:
        return
    _write_daemon_record(
        _HostDaemonRecord(
            **{
                **asdict(record),
                "resolved_server_url": server_url.rstrip("/"),
            }
        )
    )

def _load_existing_host_id() -> str | None:
    """
    Load the existing local host id without creating one.

    :returns: Host id from config, e.g. ``"host_abc123"``, or ``None``.
    """
    candidate_paths = [_effective_global_config_path()]
    from omnigent.host.identity import CONFIG_PATH

    if CONFIG_PATH not in candidate_paths:
        candidate_paths.append(CONFIG_PATH)
    for path in candidate_paths:
        try:
            raw = yaml.safe_load(path.read_text()) if path.exists() else None
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        host = raw.get("host")
        if isinstance(host, dict):
            host_id = host.get("host_id")
            if isinstance(host_id, str) and host_id:
                return host_id
    return None

def _daemon_tunnel_recovers(
    record: _HostDaemonRecord,
    *,
    grace_s: float = _DAEMON_RECONNECT_GRACE_S,
) -> bool:
    """
    Return whether a daemon's host tunnel is (or quickly becomes) online.

    Probes the host status immediately, then polls for up to *grace_s* to
    let a daemon mid-reconnect (after a transient tunnel drop) re-register
    before we judge it a zombie.

    :param record: Daemon record to probe.
    :param grace_s: Seconds to keep polling for recovery, e.g. ``5.0``.
    :returns: ``True`` if the host reports online within the grace window.
    """
    if _daemon_host_online(record):
        return True
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _daemon_host_online(record):
            return True
    return False

def _daemon_host_identity_changed(record: _HostDaemonRecord) -> bool:
    """
    Return whether a daemon record belongs to a different current host id.

    A live daemon can outlast edits to ``~/.omnigent/config.yaml``. Reusing
    that process leaves commands polling for the new host id while the daemon
    is still connected as the old host id, which can never succeed.

    :param record: Daemon record being considered for reuse.
    :returns: ``True`` when the record has a host id and the current config
        either has a different id or no id.
    """
    if record.host_id is None:
        return False
    current_host_id = _load_existing_host_id()
    return record.host_id != current_host_id

def _terminate_host_unit(record: _HostDaemonRecord, *, reason: str) -> None:
    """
    Tear down a daemon and, in local mode, the Omnigent server it owns.

    The ``--local`` daemon spawns its Omnigent server once and never respawns
    it, so a stale daemon and its server must be replaced as a unit:
    killing only the daemon would strand the server (and vice versa). This
    stops both so the caller can spawn a fresh, correctly-configured pair.

    :param record: Daemon record to tear down.
    :param reason: Human-readable reason surfaced to the user, e.g.
        ``"config changed (auth)"`` or ``"host tunnel is offline"``.
    :returns: None.
    """
    click.echo(f"Restarting host daemon for {record.target!r} ({reason}).", err=True)
    # Best-effort: a daemon that refuses to die shouldn't hard-fail the
    # run — the fresh daemon's record overwrites this one regardless.
    with contextlib.suppress(click.ClickException):
        _terminate_daemon(record, force=True)
    if record.mode == "local":
        stop_local_omnigent_server()

@dataclass(frozen=True)
class _DaemonReuseDecision:
    """Outcome of evaluating whether an existing daemon can be reused.

    :param reuse: ``True`` when the existing daemon is live, config-matching,
        and tunnel-healthy, so the caller should NOT spawn a new one.
    :param config_changed: ``True`` when the existing daemon was torn down
        specifically because its config signature no longer matches this
        invocation (e.g. the user flipped ``OMNIGENT_AUTH_ENABLED``).
        Distinct from a transparent tunnel-health heal — only a config
        change forces the caller to ask the user to re-run, because the
        server was restarted into a different auth posture mid-command.
    """

    reuse: bool
    config_changed: bool

def _reuse_existing_daemon_record(target: str) -> _DaemonReuseDecision:
    """
    Decide whether an existing daemon for *target* can be reused.

    Reuse requires more than a live PID: a daemon whose process is alive
    but whose server tunnel is down (server restart, ungraceful death,
    flapping tunnel) is a zombie — the host reads ``offline`` and the
    caller would poll until timeout. And a daemon spawned under a
    different server config (e.g. the user flipped
    ``OMNIGENT_AUTH_ENABLED``) would silently keep its old auth
    mode. In both cases we tear the unit down here and return
    ``reuse=False`` so the caller spawns a fresh one — flagging
    ``config_changed`` for the auth-drift case so the caller can ask the
    user to re-run against the freshly-restarted server.

    Self-healing is limited to daemons this CLI spawned in the background
    (they carry a ``log_path``). Foreground ``host`` daemons
    (``log_path is None``) and legacy records (``config_sig is None``) are
    never silently killed — we don't tear down an interactive process or
    one whose config we can't verify.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :returns: A :class:`_DaemonReuseDecision`.
    """
    existing = _find_daemon_record(target)
    if existing is None:
        return _DaemonReuseDecision(reuse=False, config_changed=False)
    if not _pid_alive(existing.pid):
        _delete_daemon_record(existing)
        return _DaemonReuseDecision(reuse=False, config_changed=False)

    background = existing.log_path is not None
    if background and _daemon_host_identity_changed(existing):
        _terminate_host_unit(existing, reason="host identity changed")
        return _DaemonReuseDecision(reuse=False, config_changed=False)

    if target != _LOCAL_DAEMON_MARKER:
        # Remote / explicit ``--server`` mode: the daemon connects to a server
        # we don't own and can't restart, so the config-signature / heal /
        # "re-run" semantics below don't apply (auth posture is the remote's
        # concern; its own reconnect loop covers transient tunnel drops). Keep
        # the original PID-liveness reuse so a live daemon for the URL is
        # reused as-is.
        return _DaemonReuseDecision(reuse=True, config_changed=False)

    if not background:
        # Foreground host / legacy host.pid: keep prior behavior — a
        # live PID is reused as-is (don't kill the user's interactive
        # process or guess at an unstamped config).
        return _DaemonReuseDecision(reuse=True, config_changed=False)

    # Config drift → the running server has the wrong auth source.
    desired_sig = server_config_signature()
    if existing.config_sig is not None and existing.config_sig != desired_sig:
        _terminate_host_unit(existing, reason="config changed (auth)")
        return _DaemonReuseDecision(reuse=False, config_changed=True)

    # Tunnel health → don't reuse a zombie. Skip very young daemons (a
    # concurrent invocation may have just spawned one still connecting). This
    # is a transparent heal, NOT a config change — the caller continues.
    age_s = time.time() - existing.started_at
    if age_s >= _DAEMON_REUSE_MIN_AGE_S and not _daemon_tunnel_recovers(existing):
        _terminate_host_unit(existing, reason="host tunnel is offline")
        return _DaemonReuseDecision(reuse=False, config_changed=False)
    return _DaemonReuseDecision(reuse=True, config_changed=False)

def _local_daemon_serves_target(target: str, server_url: str | None) -> bool:
    """
    Check whether the local daemon already serves a requested URL target.

    :param target: Normalized daemon target, e.g.
        ``"http://127.0.0.1:8123"``.
    :param server_url: Requested server URL, or ``None`` for local mode.
    :returns: ``True`` if the live local daemon already serves *target*.
    """
    if not server_url:
        return False
    local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if local_record is None or not _pid_alive(local_record.pid):
        return False
    local_url = local_server_url_if_healthy()
    return local_url is not None and local_url.rstrip("/") == target

def _spawn_host_daemon_process(
    *,
    args: list[str],
    env: dict[str, str],
) -> _SpawnedDaemonProcess | None:
    """
    Spawn the background host daemon and attach its log file.

    :param args: Process argv, e.g. ``["python", "-m", "..."]``.
    :param env: Allowlisted daemon environment.
    :returns: Spawned process metadata, or ``None`` if spawn fails.
    """
    log_dir = _HOST_PID_PATH.parent / "logs" / "host-daemon"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fd, log_path = tempfile.mkstemp(prefix="daemon-", suffix=".log", dir=log_dir)
    log_fh = os.fdopen(log_fd, "wb")
    try:
        proc = subprocess.Popen(
            args,
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    except OSError:
        return None
    finally:
        log_fh.close()
    return _SpawnedDaemonProcess(pid=proc.pid, log_path=log_path)

def _persist_spawned_daemon(
    *,
    target: str,
    spawned: _SpawnedDaemonProcess,
    config_sig: str,
) -> None:
    """
    Persist registry and legacy pidfile entries for a spawned daemon.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :param spawned: Spawned process metadata.
    :param config_sig: Config signature this daemon was spawned under,
        e.g. ``"3f9a1c2b4d5e6f70"`` (see :func:`server_config_signature`).
    """
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    _write_daemon_record(
        _HostDaemonRecord(
            pid=spawned.pid,
            target=target,
            mode=mode,
            server_url=None if mode == "local" else target,
            log_path=spawned.log_path,
            started_at=int(time.time()),
            host_id=_load_existing_host_id(),
            config_sig=config_sig,
        )
    )
    _HOST_PID_PATH.write_text(f"{spawned.pid}\n{target}\n")

def _foreground_daemon_record(
    *,
    target: str,
    server_url: str,
    host_id: str | None,
) -> _HostDaemonRecord:
    """
    Build the registry record for the current foreground host process.

    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :param server_url: Concrete Omnigent server URL being connected to, e.g.
        ``"http://127.0.0.1:8123"``.
    :param host_id: Local host id, e.g. ``"host_abc123"``.
    :returns: Daemon registry record for ``os.getpid()``.
    """
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    return _HostDaemonRecord(
        pid=os.getpid(),
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=int(time.time()),
        host_id=host_id,
        resolved_server_url=server_url.rstrip("/") if mode == "local" else None,
        config_sig=server_config_signature(),
    )

def _live_daemon_conflict(record: _HostDaemonRecord) -> _HostDaemonRecord | None:
    """
    Find a live daemon that already serves a foreground record target.

    :param record: Foreground daemon record this process wants to claim.
    :returns: Conflicting live record, or ``None``.
    """
    existing = _find_daemon_record(record.target)
    if existing is not None and existing.pid != record.pid and _pid_alive(existing.pid):
        return existing
    if record.mode == "server" and record.server_url is not None:
        local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
        if (
            local_record is not None
            and local_record.pid != record.pid
            and _pid_alive(local_record.pid)
            and local_record.resolved_server_url == record.server_url.rstrip("/")
        ):
            return local_record
    return None

def _claim_foreground_daemon_record(
    record: _HostDaemonRecord,
) -> _HostDaemonRecord | None:
    """
    Persist a foreground daemon record unless a live duplicate exists.

    :param record: Foreground process record, e.g. one with
        ``pid == os.getpid()``.
    :returns: Previous record for the same target, or ``None``.
    :raises click.ClickException: If a live daemon already serves the
        same target.
    """
    conflict = _live_daemon_conflict(record)
    if conflict is not None:
        raise click.ClickException(
            "A host daemon is already running for this server "
            f"(pid={conflict.pid}, target={conflict.target}). "
            "Run `omnigent host status` to inspect it or "
            "`omnigent host stop --server ...` to stop it first."
        )
    previous = _find_daemon_record(record.target)
    if previous is not None and not _pid_alive(previous.pid):
        _delete_daemon_record(previous)
        previous = None
    _write_daemon_record(record)
    return previous

def _restore_replaced_daemon_record(
    record: _HostDaemonRecord,
    previous: _HostDaemonRecord | None,
) -> None:
    """
    Restore the record replaced by a foreground host process.

    If another process has already written a newer record for the same
    target, this function leaves it untouched.

    :param record: Foreground daemon record written by this process.
    :param previous: Previous record returned by
        :func:`_claim_foreground_daemon_record`, or ``None``.
    """
    current = _read_daemon_record(_daemon_record_path(record.target))
    if current is None:
        return
    if current.pid != record.pid or current.started_at != record.started_at:
        return
    if previous is None:
        _delete_daemon_record(record)
        return
    _write_daemon_record(previous)

def _load_or_create_host_id() -> str | None:
    """
    Load or create the host id used by a foreground host process.

    :returns: Host id from local config, e.g. ``"host_abc123"``, or
        ``None`` if the identity file cannot be created.
    """
    host_id = _load_existing_host_id()
    if host_id is not None:
        return host_id
    from omnigent.host.identity import CONFIG_PATH, load_or_create_host_identity

    try:
        return load_or_create_host_identity(CONFIG_PATH).host_id
    except OSError:
        return None

def _ensure_host_daemon(server_url: str | None) -> bool:
    """Start or reuse a host daemon for one target.

    :param server_url: Omnigent server URL the daemon connects to, or ``None``
        for local mode — the daemon starts (or reuses) a persistent local
        Omnigent server and connects to that.
    :returns: ``True`` when an existing daemon was torn down and respawned
        because its config (auth source) changed — the caller
        should ask the user to re-run against the freshly-restarted server
        rather than continue this command mid-restart. ``False`` for a
        plain reuse, a transparent tunnel-health heal, or a first spawn.
    """
    target = _normalize_daemon_target(server_url)
    decision = _reuse_existing_daemon_record(target)
    if decision.reuse:
        return False
    if not decision.config_changed and _local_daemon_serves_target(target, server_url):
        return False

    _HOST_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    mode_args = ["--local"] if not server_url else ["--server", server_url]
    args = [sys.executable, "-m", "omnigent.host._daemon_entry", *mode_args]
    spawned = _spawn_host_daemon_process(
        args=args, env=_build_host_daemon_env(server_url=server_url)
    )
    if spawned is None:
        return False
    _persist_spawned_daemon(
        target=target,
        spawned=spawned,
        config_sig=server_config_signature(),
    )
    return decision.config_changed

def _build_host_daemon_env(
    *,
    server_url: str | None,
) -> dict[str, str]:
    """
    Build the environment for the background host daemon.

    Remote daemons connect to an already-running Omnigent server, so they only
    need process essentials, TLS trust, and Databricks auth. Local daemons
    also start the local Omnigent server; that server is the user's local runtime
    and must inherit Omnigent config plus provider credentials such as
    ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``. Both modes are allowlisted:
    local mode carries the runtime/provider vars needed by the local server,
    but unrelated shell secrets are not inherited merely because the daemon
    runs on the user's machine. Runners launched by the daemon still pass
    through :func:`omnigent.host.connect._build_runner_env`, so these
    local-server credentials do not leak into runner subprocesses.

    :param server_url: Omnigent server URL for remote mode, e.g.
        ``"https://example.databricksapps.com"``, or a falsey value
        such as ``None`` / ``""`` for local daemon mode.
    :returns: Environment dict for ``subprocess.Popen``.
    """
    from omnigent.host.connect import (
        _RUNNER_ENV_ALLOWLIST,
        _RUNNER_ENV_ALLOWLIST_PREFIXES,
    )

    if not server_url:
        daemon_env_prefixes = (*_RUNNER_ENV_ALLOWLIST_PREFIXES, *_LOCAL_DAEMON_ENV_PREFIXES)
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _RUNNER_ENV_ALLOWLIST
            or key in _LOCAL_DAEMON_ENV_ALLOWLIST
            or key.startswith(daemon_env_prefixes)
        }
    else:
        # Allowlist the remote daemon's environment (W8): pass process
        # essentials + TLS trust + the user's Databricks auth (the daemon
        # authenticates to the server with it), but not unrelated provider
        # secrets like ANTHROPIC_API_KEY / OPENAI_API_KEY.
        daemon_env_prefixes = (*_RUNNER_ENV_ALLOWLIST_PREFIXES, "DATABRICKS_")
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _RUNNER_ENV_ALLOWLIST or key.startswith(daemon_env_prefixes)
        }
    return env

def _read_host_pid_file() -> tuple[int, str] | None:
    """Read the host daemon PID file (two lines: PID and server URL).

    :returns: ``(pid, server_url)`` if well-formed, ``None`` otherwise.
    """
    if not _HOST_PID_PATH.exists():
        return None
    try:
        lines = _HOST_PID_PATH.read_text().strip().splitlines()
        if len(lines) < 2:
            return None
        return int(lines[0]), lines[1]
    except (ValueError, OSError):
        return None

def _host_daemon_alive() -> bool:
    """Check whether the local-mode host daemon is still alive.

    :returns: ``True`` if a local daemon record exists and its process
        is running.
    """
    existing = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if existing is None:
        return False
    return _pid_alive(existing.pid)
