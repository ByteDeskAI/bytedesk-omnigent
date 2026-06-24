"""Unit-level edge coverage for :mod:`omnigent.runtime.harnesses._runner`."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn

from omnigent.runtime.harnesses import _runner


@pytest.fixture(autouse=True)
def _reset_hard_exit_armed() -> None:
    """Isolate the module-global hard-exit guard between tests."""
    _runner._HARD_EXIT_ARMED = False


def test_set_pdeathsig_noop_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_set_pdeathsig`` is a no-op outside Linux."""
    monkeypatch.setattr(_runner.sys, "platform", "darwin")
    _runner._set_pdeathsig()


def test_set_pdeathsig_calls_prctl_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux builds attempt ``prctl(PR_SET_PDEATHSIG, SIGKILL)``."""
    monkeypatch.setattr(_runner.sys, "platform", "linux")
    calls: list[tuple[Any, ...]] = []

    class _Libc:
        def prctl(self, *args: Any) -> int:
            calls.append(args)
            return 0

    monkeypatch.setitem(sys.modules, "ctypes", MagicMock(CDLL=MagicMock(return_value=_Libc())))
    _runner._set_pdeathsig()
    assert calls
    assert calls[0][0] == 1
    assert calls[0][1] == signal.SIGKILL


def test_set_pdeathsig_swallows_prctl_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable ``prctl`` must not crash runner startup."""
    monkeypatch.setattr(_runner.sys, "platform", "linux")

    def _raise_oserror(_name: str, **_kwargs: Any) -> None:
        raise OSError("no libc")

    monkeypatch.setitem(
        sys.modules,
        "ctypes",
        MagicMock(CDLL=_raise_oserror),
    )
    _runner._set_pdeathsig()


def test_arm_hard_exit_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only one hard-exit timer arms per process."""
    started: list[str] = []
    monkeypatch.setattr(_runner, "_HARD_EXIT_ARMED", False)
    monkeypatch.setattr(
        _runner.threading,
        "Thread",
        lambda target, name, daemon: started.append(name) or MagicMock(start=MagicMock()),
    )
    _runner._arm_hard_exit("first")
    _runner._arm_hard_exit("second")
    assert started == ["harness-hard-exit"]


def test_arm_hard_exit_thread_forces_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard-exit timer calls ``os._exit`` after the deadline."""
    exits: list[int] = []
    done = threading.Event()
    monkeypatch.setattr(_runner, "_HARD_EXIT_ARMED", False)
    monkeypatch.setattr(_runner.time, "sleep", lambda _seconds: None)

    def _exit(code: int) -> None:
        exits.append(code)
        done.set()

    monkeypatch.setattr(_runner.os, "_exit", _exit)
    _runner._arm_hard_exit("wedged shutdown", signal.SIGTERM)
    assert done.wait(timeout=2)
    assert exits == [128 + int(signal.SIGTERM)]


def test_request_shutdown_with_hard_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown requests arm the hard-exit timer and self-SIGTERM."""
    armed: list[str] = []
    signals: list[int] = []
    monkeypatch.setattr(_runner, "_arm_hard_exit", lambda reason, sig=signal.SIGTERM: armed.append(reason))
    monkeypatch.setattr(_runner.os, "kill", lambda _pid, sig: signals.append(sig))
    _runner._request_shutdown_with_hard_exit("parent process exit")
    assert armed == ["parent process exit"]
    assert signals == [signal.SIGTERM]


def test_parent_watchdog_exits_when_ppid_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reparenting away from the spawning parent triggers shutdown."""
    monkeypatch.setattr(_runner, "_PARENT_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(_runner.os, "getppid", lambda: 999)
    requested: list[str] = []
    monkeypatch.setattr(
        _runner,
        "_request_shutdown_with_hard_exit",
        lambda reason: requested.append(reason),
    )
    thread = _runner._start_parent_watchdog(12345)
    thread.join(timeout=2)
    assert requested == ["parent process exit"]


def test_parent_watchdog_exits_when_parent_pid_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead parent PID triggers shutdown even if ``getppid`` is unchanged."""
    monkeypatch.setattr(_runner, "_PARENT_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(_runner.os, "getppid", lambda: 12345)

    def _kill(pid: int, sig: int) -> None:
        del sig
        if pid == 12345:
            raise ProcessLookupError(pid)

    requested: list[str] = []
    monkeypatch.setattr(_runner.os, "kill", _kill)
    monkeypatch.setattr(
        _runner,
        "_request_shutdown_with_hard_exit",
        lambda reason: requested.append(reason),
    )
    thread = _runner._start_parent_watchdog(12345)
    thread.join(timeout=2)
    assert requested == ["parent process exit"]


def test_parent_watchdog_treats_permission_error_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PermissionError`` from ``os.kill`` means the parent still exists."""
    monkeypatch.setattr(_runner, "_PARENT_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(_runner.os, "getppid", lambda: 12345)
    calls = {"count": 0}

    def _kill(pid: int, sig: int) -> None:
        del pid, sig
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError
        raise ProcessLookupError

    requested: list[str] = []
    monkeypatch.setattr(_runner.os, "kill", _kill)
    monkeypatch.setattr(
        _runner,
        "_request_shutdown_with_hard_exit",
        lambda reason: requested.append(reason),
    )
    thread = _runner._start_parent_watchdog(12345)
    thread.join(timeout=2)
    assert requested == ["parent process exit"]
    assert calls["count"] == 2


def test_hard_exit_server_handle_exit_arms_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal handling arms the hard-exit backstop before uvicorn shutdown."""
    armed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        _runner,
        "_arm_hard_exit",
        lambda reason, sig: armed.append((reason, sig)),
    )
    called: list[int] = []
    monkeypatch.setattr(
        uvicorn.Server,
        "handle_exit",
        lambda self, sig, frame: called.append(sig),
    )
    server = _runner._HardExitServer(uvicorn.Config(MagicMock()))
    server.handle_exit(signal.SIGINT, None)
    assert armed == [(f"signal {signal.SIGINT}", signal.SIGINT)]
    assert called == [signal.SIGINT]


def test_main_module_entrypoint_invokes_main() -> None:
    """The ``if __name__ == '__main__'`` guard delegates to ``main``."""
    called: list[bool] = []
    source = Path(_runner.__file__).read_text()
    source = source.replace("    main()", "    __test_main_hook__()", 1)
    namespace = dict(vars(_runner))
    namespace["__name__"] = "__main__"
    namespace["__test_main_hook__"] = lambda: called.append(True)
    exec(compile(source, _runner.__file__, "exec"), namespace)
    assert called == [True]


def test_main_starts_server_without_parent_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` loads the harness app and runs uvicorn when no parent pid is set."""
    created: list[uvicorn.Config] = []
    ran: list[bool] = []

    class _FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            created.append(config)

        def run(self) -> None:
            ran.append(True)

    monkeypatch.setattr(_runner, "_HardExitServer", _FakeServer)
    _runner.main(
        [
            "--harness",
            "test",
            "--module",
            "tests.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_main",
        ]
    )
    assert len(created) == 1
    assert created[0].uds == "/tmp/example.sock"
    assert created[0].app.state.conversation_id == "conv_main"
    assert ran == [True]


def test_main_starts_parent_watchdog_when_parent_pid_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` enables parent-death handling when ``--parent-pid`` is supplied."""
    watchdogs: list[int] = []
    pdeathsig_calls: list[bool] = []
    monkeypatch.setattr(_runner, "_set_pdeathsig", lambda: pdeathsig_calls.append(True))
    monkeypatch.setattr(
        _runner,
        "_start_parent_watchdog",
        lambda pid: watchdogs.append(pid) or MagicMock(),
    )
    monkeypatch.setattr(_runner, "_HardExitServer", lambda _config: MagicMock(run=MagicMock()))
    _runner.main(
        [
            "--harness",
            "test",
            "--module",
            "tests.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_parent",
            "--parent-pid",
            "4242",
        ]
    )
    assert pdeathsig_calls == [True]
    assert watchdogs == [4242]