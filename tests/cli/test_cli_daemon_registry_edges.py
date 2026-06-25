"""Edge tests for daemon registry helpers in :mod:`omnigent.cli`."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.cli as cli_mod
from omnigent.cli import (
    _delete_daemon_record,
    _find_daemon_record,
    _HostDaemonRecord,
    _legacy_daemon_record,
    _list_daemon_records,
    _read_daemon_record,
    _update_daemon_resolved_server_url,
    _write_daemon_record,
)


def _record(
    *,
    target: str = "local",
    pid: int = 4242,
    started_at: int = 100,
) -> _HostDaemonRecord:
    mode = "local" if target == "local" else "server"
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=started_at,
        host_id="host-1",
        resolved_server_url=None,
        config_sig=None,
    )


def test_list_daemon_records_merges_json_and_legacy_pidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")

    _write_daemon_record(_record(target="https://remote.example", pid=11, started_at=200))
    (root / "host.pid").write_text("99\nlocal\n")

    records = _list_daemon_records()
    targets = {record.target: record.pid for record in records}
    assert targets["https://remote.example"] == 11
    assert targets["local"] == 99
    assert records[0].started_at >= records[-1].started_at


def test_list_daemon_records_skips_legacy_when_json_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")
    (root / "host.pid").write_text("77\nlocal\n")
    _write_daemon_record(_record(target="local", pid=55, started_at=300))

    records = _list_daemon_records()
    assert len(records) == 1
    assert records[0].pid == 55


def test_find_daemon_record_returns_match_by_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")
    _write_daemon_record(_record(target="https://remote.example", pid=12))

    found = _find_daemon_record("https://remote.example")
    assert found is not None
    assert found.pid == 12
    assert _find_daemon_record("missing") is None


def test_legacy_daemon_record_builds_local_mode_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    root.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")
    monkeypatch.setattr(cli_mod, "_load_existing_host_id", lambda: "host-legacy")
    (root / "host.pid").write_text("88\nlocal\n")

    record = _legacy_daemon_record()
    assert record is not None
    assert record.pid == 88
    assert record.target == "local"
    assert record.mode == "local"
    assert record.host_id == "host-legacy"


def test_delete_daemon_record_removes_json_and_matching_legacy_pidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    pid_path = root / "host.pid"
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", pid_path)

    record = _record(target="local", pid=66)
    _write_daemon_record(record)
    pid_path.write_text("66\nlocal\n")

    _delete_daemon_record(record)

    assert not cli_mod._daemon_record_path("local").exists()
    assert not pid_path.exists()


def test_update_daemon_resolved_server_url_persists_normalized_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")
    _write_daemon_record(_record(target="local", pid=44))

    _update_daemon_resolved_server_url("local", "http://127.0.0.1:8123/")

    loaded = _read_daemon_record(cli_mod._daemon_record_path("local"))
    assert loaded is not None
    assert loaded.resolved_server_url == "http://127.0.0.1:8123"


def test_update_daemon_resolved_server_url_noops_for_missing_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "omnigent"
    registry = root / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", root / "host.pid")

    _update_daemon_resolved_server_url("missing", "http://127.0.0.1:8123")

    assert list(registry.glob("*.json")) == []
