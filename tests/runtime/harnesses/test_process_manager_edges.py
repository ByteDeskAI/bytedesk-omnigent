"""Edge-case coverage for :mod:`omnigent.runtime.harnesses.process_manager`.

Exercises error paths, helper functions, and branch arms that the
integration-style tests in ``test_process_manager.py`` do not hit:
spawn failures, cancel forwarding, orphan-sweep skips, reaper
in-flight guards, and subprocess teardown escalation.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
pytest_plugins = ("tests.runtime.harnesses.test_process_manager",)

from omnigent.runtime.harnesses.process_manager import (
    _AP_PID_FILE,
    _TMP_PARENT,
    _TMP_PARENT_ENV_VAR,
    HarnessProcessManager,
    _SubprocessEntry,
    _can_connect_uds,
    _default_tmp_parent,
    _pid_alive,
    _pids_holding_socket,
    _resolve_module_path,
    _wait_for_socket_bind,
)

_TEST_HARNESS_NAME = "test"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_harness"


# ── Module-level helpers ───────────────────────────────────────


def test_default_tmp_parent_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the env var is unset, the default parent is ``/tmp/omnigent``."""
    monkeypatch.delenv(_TMP_PARENT_ENV_VAR, raising=False)
    assert _default_tmp_parent() == _TMP_PARENT


def test_resolve_module_path_unknown_with_registry() -> None:
    """Unknown harness names list the registered alternatives."""
    with pytest.raises(RuntimeError, match="registered names"):
        _resolve_module_path("definitely-not-registered-harness")


def test_resolve_module_path_unknown_empty_registry() -> None:
    """An empty registry produces the Phase-1 empty-registry message."""
    saved = dict(_HARNESS_MODULES)
    _HARNESS_MODULES.clear()
    try:
        with pytest.raises(RuntimeError, match="registry is empty"):
            _resolve_module_path("anything")
    finally:
        _HARNESS_MODULES.update(saved)


async def test_wait_for_socket_bind_subprocess_exits(
    short_tmp_parent: Path,
) -> None:
    """Spawn failure when the runner subprocess exits before binding."""
    socket_path = short_tmp_parent / "conv-exit.sock"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "raise SystemExit(7)",
    )
    with pytest.raises(RuntimeError, match="exited with 7"):
        await _wait_for_socket_bind(process, socket_path, "test", "conv_exit")


async def test_wait_for_socket_bind_times_out(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn failure when the socket never appears within the deadline."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    monkeypatch.setattr(pm_mod, "_SPAWN_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pm_mod, "_SPAWN_POLL_INTERVAL_S", 0.01)
    socket_path = short_tmp_parent / "conv-timeout.sock"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    )
    with pytest.raises(RuntimeError, match="did not bind socket"):
        await _wait_for_socket_bind(process, socket_path, "test", "conv_timeout")
    if process.returncode is None:
        process.kill()
        await process.wait()


async def test_can_connect_uds_oserror_returns_false(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OSError`` from ``open_unix_connection`` is treated as not ready."""
    async def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("not a socket")

    monkeypatch.setattr(asyncio, "open_unix_connection", _raise_oserror)
    assert await _can_connect_uds(short_tmp_parent / "missing.sock") is False


def test_pid_alive_permission_error_means_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PermissionError`` on signal probe counts as alive (leave sibling alone)."""
    def _raise_permission(_pid: int, _sig: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("omnigent.runtime.harnesses.process_manager.os.kill", _raise_permission)
    assert _pid_alive(424242) is True


async def test_pids_holding_socket_parses_multiline_output(
    monkeypatch: pytest.MonkeyPatch,
    short_tmp_parent: Path,
) -> None:
    """``lsof`` stdout is parsed into integer PIDs, skipping blanks and junk."""
    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"4242\n\nnot-a-pid\n5252\n", b""

    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    pids = await _pids_holding_socket(short_tmp_parent / "conv.sock")
    assert pids == [4242, 5252]


# ── HarnessProcessManager surface ──────────────────────────────


async def test_socket_path_returns_expected_location(
    manager: HarnessProcessManager,
) -> None:
    """``socket_path`` mirrors the per-conversation socket naming scheme."""
    await manager.start()
    try:
        expected = manager.instance_dir / "conv-conv_path.sock"
        assert manager.socket_path("conv_path") == expected
    finally:
        await manager.shutdown()


async def test_has_session_and_has_active_turn(
    manager: HarnessProcessManager,
    register_test_harness: None,
) -> None:
    """Registry helpers reflect entries and in-flight response ids."""
    await manager.start()
    try:
        assert not manager.has_session("conv_flags")
        assert not manager.has_active_turn("conv_flags")

        await manager.get_client("conv_flags", _TEST_HARNESS_NAME)
        assert manager.has_session("conv_flags")

        manager._in_flight_response_ids["conv_flags"] = "resp_test"
        assert manager.has_active_turn("conv_flags")
    finally:
        await manager.shutdown()


async def test_release_noop_when_missing(manager: HarnessProcessManager) -> None:
    """``release`` on an unknown conversation id is a silent no-op."""
    await manager.start()
    try:
        await manager.release("conv_never_spawned")
    finally:
        await manager.shutdown()


async def test_get_client_unlinks_stale_socket_before_spawn(
    manager: HarnessProcessManager,
    register_test_harness: None,
) -> None:
    """A leftover socket file from a prior spawn is removed before rebinding."""
    await manager.start()
    try:
        stale = manager.socket_path("conv_stale_sock")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale", encoding="utf-8")
        await manager.get_client("conv_stale_sock", _TEST_HARNESS_NAME)
        assert stale.exists()
        response = await (await manager.get_client("conv_stale_sock", _TEST_HARNESS_NAME)).get(
            "/health"
        )
        assert response.status_code == 200
    finally:
        await manager.shutdown()


async def test_close_entry_sigkill_when_sigterm_wedges(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_close_entry`` escalates to SIGKILL when SIGTERM grace expires."""
    class _WedgedProcess:
        returncode = None
        killed = False

        def send_signal(self, _sig: signal.Signals) -> None:
            return None

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    async def _timeout_wait_for(coro: object, timeout: float) -> object:
        del coro, timeout
        raise asyncio.TimeoutError

    wedged = _WedgedProcess()
    entry = _SubprocessEntry(
        process=wedged,  # type: ignore[arg-type]
        client=httpx.AsyncClient(),
        socket_path=Path("/tmp/fake-wedged.sock"),
        harness=_TEST_HARNESS_NAME,
    )
    monkeypatch.setattr(asyncio, "wait_for", _timeout_wait_for)
    await manager._close_entry(entry)
    assert wedged.killed is True


async def test_idle_reaper_skips_in_flight_entries(
    register_test_harness: None,
    short_tmp_parent: Path,
) -> None:
    """Entries with an in-flight response id are never reaped mid-turn."""
    fast = HarnessProcessManager(
        idle_timeout_s=0.05,
        reaper_interval_s=0.05,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_inflight", _TEST_HARNESS_NAME)
        entry = fast._entries["conv_inflight"]
        entry.last_used_at = 0.0
        fast._in_flight_response_ids["conv_inflight"] = "resp_inflight"
        await asyncio.sleep(0.25)
        assert "conv_inflight" in fast._entries
    finally:
        await fast.shutdown()


async def test_forward_cancel_no_in_flight_is_noop(
    manager: HarnessProcessManager,
) -> None:
    """Without an in-flight response id, cancel forwarding returns ``False``."""
    await manager.start()
    try:
        assert await manager.forward_cancel("conv_no_turn") is False
    finally:
        await manager.shutdown()


async def test_forward_cancel_missing_entry_returns_false(
    manager: HarnessProcessManager,
) -> None:
    """In-flight id without a subprocess entry is a defensive no-op."""
    await manager.start()
    try:
        manager._in_flight_response_ids["conv_orphan"] = "resp_orphan"
        assert await manager.forward_cancel("conv_orphan") is False
    finally:
        await manager.shutdown()


async def test_forward_cancel_transport_error_returns_false(
    manager: HarnessProcessManager,
    register_test_harness: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP transport failures are logged and return ``False``."""
    await manager.start()
    try:
        client = await manager.get_client("conv_cancel_err", _TEST_HARNESS_NAME)
        manager._in_flight_response_ids["conv_cancel_err"] = "resp_err"

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("uds gone")

        monkeypatch.setattr(client, "post", _boom)
        assert await manager.forward_cancel("conv_cancel_err") is False
    finally:
        await manager.shutdown()


async def test_forward_cancel_harness_error_status_returns_false(
    manager: HarnessProcessManager,
    register_test_harness: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness 4xx responses are logged and return ``False``."""
    await manager.start()
    try:
        client = await manager.get_client("conv_cancel_4xx", _TEST_HARNESS_NAME)
        manager._in_flight_response_ids["conv_cancel_4xx"] = "resp_4xx"

        bad_response = MagicMock(spec=httpx.Response)
        bad_response.status_code = 500
        monkeypatch.setattr(client, "post", AsyncMock(return_value=bad_response))
        assert await manager.forward_cancel("conv_cancel_4xx") is False
    finally:
        await manager.shutdown()


async def test_forward_cancel_success_returns_true(
    manager: HarnessProcessManager,
    register_test_harness: None,
) -> None:
    """A harness 2xx on the interrupt event returns ``True``."""
    await manager.start()
    try:
        await manager.get_client("conv_cancel_ok", _TEST_HARNESS_NAME)
        manager._in_flight_response_ids["conv_cancel_ok"] = "resp_ok"
        assert await manager.forward_cancel("conv_cancel_ok") is True
    finally:
        await manager.shutdown()


# ── Orphan sweep edge cases ────────────────────────────────────


async def test_sweep_orphans_missing_tmp_parent(short_tmp_parent: Path) -> None:
    """Sweep is a no-op when the tmp parent directory does not exist."""
    missing_parent = short_tmp_parent / "does-not-exist"
    mgr = HarnessProcessManager(tmp_parent=missing_parent)
    await mgr._sweep_orphans()


async def test_sweep_orphans_skips_non_ap_dirs(short_tmp_parent: Path) -> None:
    """Only ``ap-*`` directories participate in the orphan sweep."""
    short_tmp_parent.mkdir(parents=True, exist_ok=True)
    (short_tmp_parent / "other-dir").mkdir()
    (short_tmp_parent / "ap-valid").mkdir()
    (short_tmp_parent / "ap-valid" / _AP_PID_FILE).write_text("99999999", encoding="utf-8")

    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._sweep_orphans()
    assert (short_tmp_parent / "other-dir").exists()


async def test_sweep_orphans_skips_dirs_without_sentinel(short_tmp_parent: Path) -> None:
    """``ap-*`` dirs missing ``AP_PID`` are left alone."""
    short_tmp_parent.mkdir(parents=True, exist_ok=True)
    no_sentinel = short_tmp_parent / "ap-no-sentinel"
    no_sentinel.mkdir()
    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._sweep_orphans()
    assert no_sentinel.exists()


async def test_sweep_orphans_skips_unreadable_sentinel(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable or non-numeric sentinels are logged and skipped."""
    short_tmp_parent.mkdir(parents=True, exist_ok=True)
    bad_dir = short_tmp_parent / "ap-bad-sentinel"
    bad_dir.mkdir()
    (bad_dir / _AP_PID_FILE).write_text("not-a-pid", encoding="utf-8")

    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._sweep_orphans()
    assert bad_dir.exists()


async def test_kill_orphan_runners_process_lookup_error(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ProcessLookupError`` while signaling an orphan is ignored."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    async def _fake_pids(_socket: Path) -> list[int]:
        return [77777]

    def _fake_kill(_pid: int, _sig: signal.Signals) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(pm_mod, "_pids_holding_socket", _fake_pids)
    monkeypatch.setattr(pm_mod.os, "kill", _fake_kill)

    instance_dir = short_tmp_parent / "ap-dead"
    instance_dir.mkdir()
    (instance_dir / "conv-stale.sock").touch()
    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._kill_orphan_runners(instance_dir)


async def test_kill_orphan_runners_skips_sigkill_when_pid_already_dead(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphans that exit after SIGTERM are not escalated to SIGKILL."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    killed: list[signal.Signals] = []

    async def _fake_pids(_socket: Path) -> list[int]:
        return [33333]

    def _fake_kill(_pid: int, sig: signal.Signals) -> None:
        killed.append(sig)

    def _fake_pid_alive(_pid: int) -> bool:
        return False

    monkeypatch.setattr(pm_mod, "_pids_holding_socket", _fake_pids)
    monkeypatch.setattr(pm_mod, "_ORPHAN_SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(pm_mod, "_pid_alive", _fake_pid_alive)
    monkeypatch.setattr(pm_mod.os, "kill", _fake_kill)

    instance_dir = short_tmp_parent / "ap-dead"
    instance_dir.mkdir()
    (instance_dir / "conv-stale.sock").touch()
    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._kill_orphan_runners(instance_dir)
    assert killed == [signal.SIGTERM]


async def test_kill_orphan_runners_permission_error_on_sigterm(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PermissionError`` on SIGTERM is logged and does not abort the sweep."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    async def _fake_pids(_socket: Path) -> list[int]:
        return [88888]

    def _fake_kill(pid: int, sig: signal.Signals) -> None:
        if sig == signal.SIGTERM:
            raise PermissionError("denied")

    monkeypatch.setattr(pm_mod, "_pids_holding_socket", _fake_pids)
    monkeypatch.setattr(pm_mod, "_ORPHAN_SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(pm_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(pm_mod.os, "kill", _fake_kill)

    instance_dir = short_tmp_parent / "ap-dead"
    instance_dir.mkdir()
    (instance_dir / "conv-stale.sock").touch()
    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._kill_orphan_runners(instance_dir)