"""Tests for native Grok bridge state helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.grok_native_bridge import (
    GrokNativeBridgeState,
    _grok_capture,
    _grok_draft_present,
    _grok_prompt_ready,
    bridge_dir_for_session_id,
    bridge_root,
    build_grok_native_spawn_env,
    clear_bridge_state,
    grok_leader_socket_for_session,
    inject_user_message,
    prepare_bridge_dir,
    read_bridge_state,
    read_tmux_target,
    write_bridge_state,
    write_tmux_target,
)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated bridge root under ``tmp_path``."""
    monkeypatch.setattr("omnigent.grok_native_bridge._BRIDGE_ROOT", tmp_path / "grok-native")
    return prepare_bridge_dir("conv_grok_test")


def test_grok_leader_socket_is_stable_per_session() -> None:
    first = grok_leader_socket_for_session("conv_a")
    second = grok_leader_socket_for_session("conv_a")
    other = grok_leader_socket_for_session("conv_b")
    assert first == second
    assert first != other
    assert first.name.startswith("leader-")


def test_bridge_root_and_dir_for_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.grok_native_bridge._BRIDGE_ROOT", tmp_path / "grok-native")
    assert bridge_root() == tmp_path / "grok-native"
    digest_dir = bridge_dir_for_session_id("conv_xyz")
    assert digest_dir.parent == tmp_path / "grok-native"
    assert digest_dir.name != "conv_xyz"


def test_build_grok_native_spawn_env_uses_per_session_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.grok_native_bridge._BRIDGE_ROOT", tmp_path / "grok-native")
    env = build_grok_native_spawn_env("conv_spawn")
    assert env["HARNESS_GROK_NATIVE_REQUEST_SESSION_ID"] == "conv_spawn"
    assert env["HARNESS_GROK_LEADER_SOCKET"] == str(grok_leader_socket_for_session("conv_spawn"))
    assert Path(env["HARNESS_GROK_NATIVE_BRIDGE_DIR"]).is_dir()


def test_build_grok_native_spawn_env_honors_explicit_leader_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.grok_native_bridge._BRIDGE_ROOT", tmp_path / "grok-native")
    env = build_grok_native_spawn_env("conv_spawn", leader_socket="/tmp/custom.sock")
    assert env["HARNESS_GROK_LEADER_SOCKET"] == "/tmp/custom.sock"


def test_bridge_state_round_trip_and_clear(bridge_dir: Path) -> None:
    state = GrokNativeBridgeState(
        session_id="conv_grok_test",
        grok_session_id="sess_grok_1",
        leader_socket="/tmp/leader.sock",
    )
    write_bridge_state(bridge_dir, state)
    loaded = read_bridge_state(bridge_dir)
    assert loaded == state
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_rejects_invalid_payload(bridge_dir: Path) -> None:
    (bridge_dir / "state.json").write_text("not-json", encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None

    (bridge_dir / "state.json").write_text(json.dumps({"session_id": ""}), encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None

    (bridge_dir / "state.json").write_text(
        json.dumps({"session_id": "x", "grok_session_id": 1}), encoding="utf-8"
    )
    assert read_bridge_state(bridge_dir) is None


def test_clear_bridge_state_is_idempotent(bridge_dir: Path) -> None:
    clear_bridge_state(bridge_dir)
    write_bridge_state(
        bridge_dir,
        GrokNativeBridgeState(
            session_id="conv_grok_test",
            grok_session_id="sess",
            leader_socket="/tmp/s.sock",
        ),
    )
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None


def test_tmux_target_round_trip(bridge_dir: Path) -> None:
    write_tmux_target(bridge_dir, socket_path="/tmp/tmux.sock", tmux_target="main:0.0")
    info = read_tmux_target(bridge_dir)
    assert info == {"socket_path": "/tmp/tmux.sock", "tmux_target": "main:0.0"}


def test_read_tmux_target_returns_none_for_missing_or_invalid(bridge_dir: Path) -> None:
    assert read_tmux_target(bridge_dir) is None
    (bridge_dir / "tmux.json").write_text('{"socket_path": ""}', encoding="utf-8")
    assert read_tmux_target(bridge_dir) is None
    (bridge_dir / "tmux.json").write_text("broken", encoding="utf-8")
    assert read_tmux_target(bridge_dir) is None


def test_grok_prompt_and_draft_helpers() -> None:
    assert _grok_prompt_ready("Grok Composer\n❯ hi") is True
    assert _grok_prompt_ready("booting...") is False
    assert _grok_draft_present("❯ hello", "hello") is True
    assert _grok_draft_present("done\n", "missing") is False


def test_grok_capture_returns_stdout_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        stdout = "pane text"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Result())
    assert _grok_capture("/tmp/s.sock", "main") == "pane text"

    def _boom(*_a, **_k):
        raise subprocess.SubprocessError("tmux missing")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _grok_capture("/tmp/s.sock", "main") == ""


def test_inject_user_message_returns_false_without_tmux_target(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.grok_native_bridge.time.sleep", lambda _s: None)
    assert inject_user_message(bridge_dir, content="hello", timeout_s=0.0) is False


def test_inject_user_message_submits_when_target_ready(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_tmux_target(bridge_dir, socket_path="/tmp/g.sock", tmux_target="main")
    calls: list[list[str]] = []

    def _fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if "capture-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="Grok Composer\n❯ \n")
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr("omnigent.grok_native_bridge.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "omnigent.grok_native_bridge._grok_draft_present",
        lambda _pane, _needle: False,
    )

    assert inject_user_message(bridge_dir, content="ship it", timeout_s=1.0) is True
    assert any("paste-buffer" in " ".join(c) for c in calls)
