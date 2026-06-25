"""Unit tests for pure helpers in :mod:`omnigent.cli`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click import ClickException

import omnigent.cli as cli_mod
from omnigent.cli import (
    _bundled_example_path,
    _daemon_record_path,
    _display_config_path,
    _display_path,
    _effective_global_config_path,
    _HostDaemonRecord,
    _is_removed_ad_hoc_invocation,
    _is_run_shorthand,
    _is_server_url,
    _load_config,
    _load_effective_config,
    _load_global_config,
    _load_local_config,
    _migrate_legacy_state_dir,
    _normalize_daemon_target,
    _parse_config_bool,
    _peek_default_agent_harness,
    _read_daemon_record,
    _record_from_json,
    _resolve_auto_open_conversation_from_config,
    _resolve_auto_open_conversation_setting,
    _runner_loopback_host,
    _save_local_config,
    _server_uvicorn_log_config,
    _should_skip_update_check,
)


def test_load_config_none_returns_empty() -> None:
    assert _load_config(None) == {}


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("harness: codex\n")
    assert _load_config(str(cfg)) == {"harness": "codex"}


def test_server_uvicorn_log_config_swaps_formatter() -> None:
    log_config = _server_uvicorn_log_config()
    assert (
        log_config["formatters"]["access"]["()"]
        == "omnigent.server.performance_metrics.RequestDurationAccessFormatter"
    )


def test_effective_global_config_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "cfg-home"))
    assert _effective_global_config_path() == tmp_path / "cfg-home" / "config.yaml"


def test_display_path_collapses_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".omnigent" / "config.yaml"
    assert _display_path(path) == "~/.omnigent/config.yaml"
    assert _display_config_path(path) == "~/.omnigent/config.yaml"


def test_display_path_outside_home_is_absolute(tmp_path: Path) -> None:
    outside = tmp_path / "state" / "config.yaml"
    assert _display_path(outside) == str(outside)


def test_load_global_config_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    assert _load_global_config() == {}


def test_load_global_config_reads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "cfg"
    home.mkdir()
    (home / "config.yaml").write_text("default_agent: agent.yaml\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home))
    assert _load_global_config() == {"default_agent": "agent.yaml"}


def test_load_local_config_reads_cwd_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    local = tmp_path / ".omnigent"
    local.mkdir()
    (local / "config.yaml").write_text("model: gpt-4o\n")
    assert _load_local_config() == {"model": "gpt-4o"}


def test_load_effective_config_local_overrides_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    global_home = tmp_path / "global"
    global_home.mkdir()
    (global_home / "config.yaml").write_text("model: global\nharness: codex\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(global_home))
    local = tmp_path / ".omnigent"
    local.mkdir()
    (local / "config.yaml").write_text("model: local\n")
    assert _load_effective_config() == {"model": "local", "harness": "codex"}


def test_peek_default_agent_harness_url_returns_none() -> None:
    assert _peek_default_agent_harness("https://example.com/agent.yaml") is None


def test_peek_default_agent_harness_reads_executor_harness(tmp_path: Path) -> None:
    agent = tmp_path / "agent.yaml"
    agent.write_text("executor:\n  harness: codex\n")
    assert _peek_default_agent_harness(str(agent)) == "codex"


def test_peek_default_agent_harness_executor_type_fallback(tmp_path: Path) -> None:
    agent = tmp_path / "agent.yaml"
    agent.write_text("executor:\n  type: claude-sdk\n")
    harness = _peek_default_agent_harness(str(agent))
    assert harness is not None


def test_peek_default_agent_harness_missing_file_returns_none(tmp_path: Path) -> None:
    assert _peek_default_agent_harness(str(tmp_path / "missing.yaml")) is None


def test_peek_default_agent_harness_invalid_yaml_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  bad: [")
    assert _peek_default_agent_harness(str(bad)) is None


def test_peek_default_agent_harness_non_dict_root_returns_none(tmp_path: Path) -> None:
    agent = tmp_path / "list.yaml"
    agent.write_text("- item\n")
    assert _peek_default_agent_harness(str(agent)) is None


def test_bundled_example_path_resolves_polly() -> None:
    path = _bundled_example_path("polly")
    assert path.endswith(("polly", "polly/"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:6767", True),
        ("https://example.com", True),
        ("agent.yaml", False),
        ("./local.yaml", False),
    ],
)
def test_is_server_url(value: str, expected: bool) -> None:
    assert _is_server_url(value) is expected


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        (None, "local"),
        ("", "local"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com", "https://example.com"),
    ],
)
def test_normalize_daemon_target(server_url: str | None, expected: str) -> None:
    assert _normalize_daemon_target(server_url) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], False),
        (["run", "agent.yaml"], False),
        (["--help"], False),
        (["what does this repo do?"], True),
        (["blah"], False),
    ],
)
def test_is_removed_ad_hoc_invocation(argv: list[str], expected: bool) -> None:
    assert _is_removed_ad_hoc_invocation(argv) is expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("::", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "localhost"),
    ],
)
def test_runner_loopback_host(host: str, expected: str) -> None:
    assert _runner_loopback_host(host) == expected


@pytest.mark.parametrize(
    ("argv", "skip"),
    [
        ([], True),
        (["run"], False),
        (["--help"], True),
        (["upgrade"], True),
        (["pane-split"], True),
    ],
)
def test_should_skip_update_check(argv: list[str], skip: bool) -> None:
    assert _should_skip_update_check(argv) is skip


def test_parse_config_bool_accepts_common_literals() -> None:
    assert _parse_config_bool("auto_open_conversation", True) is True
    assert _parse_config_bool("auto_open_conversation", "yes") is True
    assert _parse_config_bool("auto_open_conversation", "off") is False


def test_parse_config_bool_rejects_invalid() -> None:
    with pytest.raises(ClickException):
        _parse_config_bool("auto_open_conversation", "maybe")


def test_resolve_auto_open_conversation_setting_tri_state() -> None:
    assert _resolve_auto_open_conversation_setting({}) is None
    assert _resolve_auto_open_conversation_setting({"auto_open_conversation": "true"}) is True


def test_resolve_auto_open_conversation_from_config_defaults_false() -> None:
    assert _resolve_auto_open_conversation_from_config({}) is False
    assert _resolve_auto_open_conversation_from_config({"auto_open_conversation": True}) is True


def test_is_run_shorthand_detects_yaml_path() -> None:
    assert _is_run_shorthand(["agent.yaml"]) is True
    assert _is_run_shorthand(["run", "agent.yaml"]) is False


def test_migrate_legacy_state_dir_noop_when_target_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "omnigent"
    state.mkdir()
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    _migrate_legacy_state_dir()
    assert legacy.exists()


def test_migrate_legacy_state_dir_moves_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "omnigent"
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("old: true\n")
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    _migrate_legacy_state_dir()
    assert state.exists()
    assert not legacy.exists()
    assert "Migrated" in capsys.readouterr().err


def test_migrate_legacy_state_dir_skips_when_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "omnigent"
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    _migrate_legacy_state_dir()
    assert legacy.exists()


def test_migrate_legacy_state_dir_skips_when_legacy_daemon_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "omnigent"
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    (legacy / "host.pid").write_text("4242\n")
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    _migrate_legacy_state_dir()
    assert legacy.exists()
    assert not state.exists()
    assert "skipping migration" in capsys.readouterr().err


def test_migrate_legacy_state_dir_tolerates_malformed_pidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "omnigent"
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    (legacy / "host.pid").write_text("not-a-pid\n")
    (legacy / "config.yaml").write_text("old: true\n")
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    monkeypatch.setattr(cli_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    _migrate_legacy_state_dir()
    assert state.exists()
    assert "Migrated" in capsys.readouterr().err


def test_migrate_legacy_state_dir_warns_on_move_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "omnigent"
    legacy = tmp_path / "omnigents"
    legacy.mkdir()
    monkeypatch.setattr(cli_mod, "_STATE_DIR", state)
    monkeypatch.setattr(cli_mod, "_LEGACY_STATE_DIRS", (legacy,))
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)

    def _boom(_src: str, _dst: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(cli_mod.shutil, "move", _boom)
    _migrate_legacy_state_dir()
    assert legacy.exists()
    err = capsys.readouterr().err
    assert "could not migrate" in err
    assert "permission denied" in err


def test_save_local_config_writes_and_unsets_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    omni = tmp_path / ".omnigent"
    omni.mkdir()
    (omni / "config.yaml").write_text("server: http://old\nkeep: yes\n")
    _save_local_config({"default_agent": "agents/demo.yaml"}, unset_keys=("server",))
    loaded = yaml.safe_load((omni / "config.yaml").read_text())
    assert loaded["default_agent"] == "agents/demo.yaml"
    assert loaded["keep"] == "yes"
    assert "server" not in loaded


def test_record_from_json_roundtrip() -> None:
    raw = {
        "pid": 4242,
        "target": "local",
        "mode": "local",
        "started_at": 1_700_000_000,
        "host_id": "host-1",
    }
    record = _record_from_json(raw)
    assert record is not None
    assert record.pid == 4242
    assert record.host_id == "host-1"


def test_record_from_json_rejects_malformed() -> None:
    assert (
        _record_from_json({"pid": "not-int", "target": "local", "mode": "local", "started_at": 1})
        is None
    )
    assert _record_from_json({"pid": 1, "target": "", "mode": "local", "started_at": 1}) is None


def test_read_daemon_record_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {"pid": 99, "target": "local", "mode": "local", "started_at": 100},
        )
    )
    record = _read_daemon_record(path)
    assert record is not None
    assert record.pid == 99


def test_daemon_record_path_is_stable_for_target() -> None:
    a = _daemon_record_path("https://example.com")
    b = _daemon_record_path("https://example.com")
    c = _daemon_record_path("local")
    assert a == b
    assert a != c
    assert a.name.endswith(".json")


def test_write_daemon_record_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = tmp_path / "daemons"
    registry.mkdir(parents=True)
    monkeypatch.setattr(cli_mod, "_HOST_PID_PATH", tmp_path / "host.pid")
    record = _HostDaemonRecord(
        pid=123,
        target="local",
        mode="local",
        server_url=None,
        log_path=None,
        started_at=1,
        host_id=None,
        resolved_server_url=None,
        config_sig=None,
    )
    cli_mod._write_daemon_record(record)
    path = _daemon_record_path("local")
    assert path.exists()
    loaded = _read_daemon_record(path)
    assert loaded is not None
    assert loaded.pid == 123
