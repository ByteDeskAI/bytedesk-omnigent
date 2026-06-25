"""Edge tests for daemon reuse decisions in :mod:`omnigent.cli`."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.cli as cli_mod
from omnigent.cli import (
    _DaemonReuseDecision,
    _HostDaemonRecord,
    _local_daemon_serves_target,
    _reuse_existing_daemon_record,
    _write_daemon_record,
)


def _background_record(
    *,
    target: str = "local",
    pid: int = 4242,
    started_at: int = 1_000_000,
    config_sig: str | None = "sig-current",
    log_path: str = "/tmp/daemon.log",
) -> _HostDaemonRecord:
    mode = "local" if target == "local" else "server"
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=log_path,
        started_at=started_at,
        host_id="host-1",
        resolved_server_url="http://127.0.0.1:8123",
        config_sig=config_sig,
    )


def test_reuse_existing_daemon_record_returns_false_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: None)
    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=False, config_changed=False)


def test_reuse_existing_daemon_record_deletes_dead_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record()
    deleted: list[_HostDaemonRecord] = []
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(cli_mod, "_delete_daemon_record", deleted.append)

    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=False, config_changed=False)
    assert deleted == [record]


def test_reuse_existing_daemon_record_reuses_remote_target_without_config_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record(target="https://remote.example", config_sig=None)
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(cli_mod, "_daemon_host_identity_changed", lambda _record: False)

    def _must_not_terminate(*_a: object, **_k: object) -> None:
        raise AssertionError("remote daemon must not be terminated during reuse")

    monkeypatch.setattr(cli_mod, "_terminate_host_unit", _must_not_terminate)
    decision = _reuse_existing_daemon_record("https://remote.example")
    assert decision == _DaemonReuseDecision(reuse=True, config_changed=False)


def test_reuse_existing_daemon_record_reuses_foreground_local_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record(log_path=None, config_sig=None)
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=True, config_changed=False)


def test_reuse_existing_daemon_record_flags_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record(config_sig="stale-signature")
    torn_down: list[str] = []
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(cli_mod, "_daemon_host_identity_changed", lambda _record: False)
    monkeypatch.setattr(cli_mod, "server_config_signature", lambda: "fresh-signature")
    monkeypatch.setattr(
        cli_mod,
        "_terminate_host_unit",
        lambda _record, *, reason: torn_down.append(reason),
    )

    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=False, config_changed=True)
    assert torn_down and "config" in torn_down[0]


def test_reuse_existing_daemon_record_heals_offline_tunnel_for_mature_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record(config_sig=cli_mod.server_config_signature())
    torn_down: list[str] = []
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(cli_mod, "_daemon_host_identity_changed", lambda _record: False)
    monkeypatch.setattr(cli_mod, "server_config_signature", cli_mod.server_config_signature)
    monkeypatch.setattr(cli_mod.time, "time", lambda: record.started_at + 60)
    monkeypatch.setattr(cli_mod, "_daemon_tunnel_recovers", lambda *_a, **_k: False)
    monkeypatch.setattr(
        cli_mod,
        "_terminate_host_unit",
        lambda _record, *, reason: torn_down.append(reason),
    )

    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=False, config_changed=False)
    assert torn_down and "offline" in torn_down[0]


def test_reuse_existing_daemon_record_reuses_young_offline_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _background_record(config_sig=cli_mod.server_config_signature())
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: record)
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(cli_mod, "_daemon_host_identity_changed", lambda _record: False)
    monkeypatch.setattr(cli_mod, "server_config_signature", cli_mod.server_config_signature)
    monkeypatch.setattr(cli_mod.time, "time", lambda: record.started_at + 1)

    def _must_not_probe(*_a: object, **_k: object) -> bool:
        raise AssertionError("young daemon must not be tunnel-probed")

    monkeypatch.setattr(cli_mod, "_daemon_tunnel_recovers", _must_not_probe)
    decision = _reuse_existing_daemon_record("local")
    assert decision == _DaemonReuseDecision(reuse=True, config_changed=False)


def test_local_daemon_serves_target_false_without_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "_find_daemon_record", lambda _target: _background_record())
    assert _local_daemon_serves_target("http://127.0.0.1:8123", None) is False


def test_local_daemon_serves_target_matches_resolved_local_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")
    _write_daemon_record(_background_record())
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        cli_mod,
        "local_server_url_if_healthy",
        lambda: "http://127.0.0.1:8123/",
    )

    assert _local_daemon_serves_target("http://127.0.0.1:8123", "http://127.0.0.1:8123") is True
    assert _local_daemon_serves_target("http://127.0.0.1:9999", "http://127.0.0.1:9999") is False
