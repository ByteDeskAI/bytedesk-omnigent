"""Edge-path coverage for :mod:`omnigent.host.connect` helpers and handlers."""

from __future__ import annotations

import asyncio
import os
import subprocess
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from omnigent.host.connect import (
    HostProcess,
    _display_log_path,
    _read_log_tail,
    _url_is_loopback,
    run_host_process,
)
from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostCreateWorktreeResultFrame,
    HostLaunchRunnerFrame,
    HostListDirFrame,
    HostRemoveWorktreeFrame,
    HostRemoveWorktreeResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.git_worktree import CreatedWorktree, WorktreeError
from omnigent.host.keepalive import (
    PingFrame,
    PongFrame,
    decode_keepalive_frame,
    encode_keepalive_frame,
)
from omnigent.host.identity import HostIdentity
from tests.host.test_connect import _ConnectSpy, _patch_connect


def _host(server_url: str = "http://127.0.0.1:8000") -> HostProcess:
    identity = HostIdentity(host_id="host_edges", name="edge-laptop")
    return HostProcess(identity, server_url)


class _JsonResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _SendOnceTunnel:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[str] = []
        self._fail_send = fail_send

    async def send(self, data: str) -> None:
        if self._fail_send:
            raise RuntimeError("send failed")
        self.sent.append(data)

    async def recv(self) -> str:
        raise ConnectionError("disconnect")


class _TimeoutThenDisconnectTunnel:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._recv_calls = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        self._recv_calls += 1
        if self._recv_calls == 1:
            raise asyncio.TimeoutError()
        raise ConnectionError("disconnect")


def test_display_log_path_outside_home() -> None:
    path = Path("/var/log/omnigent/runner.log")
    assert _display_log_path(path) == str(path)


def test_read_log_tail_returns_empty_on_os_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.log"
    assert _read_log_tail(missing) == ""


def test_url_is_loopback_returns_false_on_unparseable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_url: str) -> object:
        raise ValueError("bad url")

    monkeypatch.setattr("urllib.parse.urlparse", _boom)
    assert _url_is_loopback("not-a-valid-url") is False


@pytest.mark.asyncio
async def test_handle_launch_spawn_oserror_returns_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    host = _host()
    workspace = tmp_path / "project"
    workspace.mkdir()
    frame = HostLaunchRunnerFrame(
        request_id="req_oserr",
        binding_token="tok",
        workspace=str(workspace),
    )

    with patch(
        "omnigent.host.connect.subprocess.Popen",
        side_effect=OSError("too many open files"),
    ):
        result = await host._handle_launch(frame)

    assert result.status == "failed"
    assert "failed to spawn runner" in (result.error or "")


def test_handle_stop_kills_runner_when_terminate_times_out(tmp_path: Path) -> None:
    host = _host()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd=["sleep"], timeout=5.0),
        0,
    ]
    host._runners["runner_kill"] = SimpleNamespace(proc=proc, log_path=tmp_path / "r.log")

    from omnigent.host.frames import HostStopRunnerFrame

    result = host._handle_stop(
        HostStopRunnerFrame(request_id="req_kill", runner_id="runner_kill")
    )

    assert result.status == "stopped"
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2


@pytest.mark.asyncio
async def test_report_runner_exit_parks_on_send_failure() -> None:
    host = _host()
    tunnel = _SendOnceTunnel(fail_send=True)
    host._ws = tunnel  # type: ignore[assignment]

    await host._report_runner_exit("runner_x", "boom")

    assert host._unreported_exits == {"runner_x": "boom"}
    assert tunnel.sent == []


def test_handle_stat_path_expansion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()

    def _boom(_path: str) -> str:
        raise TypeError("bad path type")

    monkeypatch.setattr("omnigent.host.connect.os.path.expanduser", _boom)
    result = host._handle_stat(HostStatFrame(request_id="req", path="~/x"))

    assert result.status == "failed"
    assert "path expansion failed" in (result.error or "")


def test_handle_stat_oserror_returns_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    target = tmp_path / "file.txt"
    target.write_text("x")

    def _boom(_path: str) -> os.stat_result:
        raise OSError(5, "I/O error")

    monkeypatch.setattr("omnigent.host.connect.os.stat", _boom)
    result = host._handle_stat(HostStatFrame(request_id="req", path=str(target)))

    assert result.status == "failed"
    assert "stat failed" in (result.error or "")


def test_handle_stat_realpath_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    target = tmp_path / "file.txt"
    target.write_text("x")

    monkeypatch.setattr(
        "omnigent.host.connect.os.path.realpath",
        MagicMock(side_effect=OSError(5, "realpath broke")),
    )
    result = host._handle_stat(HostStatFrame(request_id="req", path=str(target)))

    assert result.status == "failed"
    assert "realpath failed" in (result.error or "")


def test_handle_stat_other_entry_type(tmp_path: Path) -> None:
    host = _host()
    fifo = tmp_path / "pipe.fifo"
    os.mkfifo(fifo)
    result = host._handle_stat(HostStatFrame(request_id="req", path=str(fifo)))

    assert result.status == "ok"
    assert result.exists is True
    assert result.type == "other"


def test_handle_list_dir_path_expansion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()

    def _boom(_path: str) -> str:
        raise ValueError("bad path")

    monkeypatch.setattr("omnigent.host.connect.os.path.expanduser", _boom)
    result = host._handle_list_dir(HostListDirFrame(request_id="req", path="~/x"))

    assert result.status == "failed"
    assert "path expansion failed" in (result.error or "")


def test_handle_list_dir_permission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host()
    root = tmp_path / "secret"
    root.mkdir()

    class _DeniedScandir:
        def __enter__(self) -> _DeniedScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> _DeniedScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            raise StopIteration

    def _denied(_path: str) -> _DeniedScandir:
        raise PermissionError("permission denied")

    monkeypatch.setattr("omnigent.host.connect.os.scandir", _denied)
    result = host._handle_list_dir(HostListDirFrame(request_id="req", path=str(root)))
    assert result.status == "ok"
    assert result.error == "permission denied"


def test_handle_list_dir_scandir_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    root = tmp_path / "root"
    root.mkdir()

    def _boom(_path: str) -> list[os.DirEntry[str]]:
        raise OSError(5, "scandir broke")

    monkeypatch.setattr("omnigent.host.connect.os.scandir", _boom)
    result = host._handle_list_dir(HostListDirFrame(request_id="req", path=str(root)))

    assert result.status == "failed"
    assert "scandir failed" in (result.error or "")


def test_handle_list_dir_other_entry_type(tmp_path: Path) -> None:
    host = _host()
    fifo = tmp_path / "pipe.fifo"
    os.mkfifo(fifo)
    result = host._handle_list_dir(HostListDirFrame(request_id="req", path=str(tmp_path)))

    names = {entry.name for entry in (result.entries or [])}
    assert "pipe.fifo" in names
    other = next(e for e in (result.entries or []) if e.name == "pipe.fifo")
    assert other.type == "other"
    assert other.bytes is None


@pytest.mark.asyncio
async def test_handle_create_worktree_success(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    created = CreatedWorktree(worktree_path="/wt/feature", branch="feature/x")

    async def _fake_to_thread(fn, **kwargs: object) -> CreatedWorktree:
        assert fn.__name__ == "create_worktree"
        return created

    monkeypatch.setattr("omnigent.host.connect.asyncio.to_thread", _fake_to_thread)
    result = await host._handle_create_worktree(
        HostCreateWorktreeFrame(
            request_id="req_wt",
            repo_path="/repo",
            branch_name="feature/x",
            base_branch="main",
        )
    )

    assert result.status == "ok"
    assert result.worktree_path == "/wt/feature"
    assert result.branch == "feature/x"


@pytest.mark.asyncio
async def test_handle_create_worktree_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()

    async def _fake_to_thread(fn, **kwargs: object) -> CreatedWorktree:
        raise WorktreeError("not a git repository")

    monkeypatch.setattr("omnigent.host.connect.asyncio.to_thread", _fake_to_thread)
    result = await host._handle_create_worktree(
        HostCreateWorktreeFrame(
            request_id="req_wt",
            repo_path="/repo",
            branch_name="feature/x",
        )
    )

    assert result.status == "failed"
    assert result.error == "not a git repository"


@pytest.mark.asyncio
async def test_handle_remove_worktree_success(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    calls: list[dict[str, object]] = []

    async def _fake_to_thread(fn, **kwargs: object) -> None:
        calls.append(kwargs)
        return None

    monkeypatch.setattr("omnigent.host.connect.asyncio.to_thread", _fake_to_thread)
    result = await host._handle_remove_worktree(
        HostRemoveWorktreeFrame(
            request_id="req_rm",
            worktree_path="/wt/feature",
            branch="feature/x",
            delete_branch=True,
        )
    )

    assert result.status == "ok"
    assert calls == [
        {
            "worktree_path": "/wt/feature",
            "branch": "feature/x",
            "delete_branch": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_remove_worktree_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()

    async def _fake_to_thread(fn, **kwargs: object) -> None:
        raise WorktreeError("worktree path missing")

    monkeypatch.setattr("omnigent.host.connect.asyncio.to_thread", _fake_to_thread)
    result = await host._handle_remove_worktree(
        HostRemoveWorktreeFrame(
            request_id="req_rm",
            worktree_path="/wt/missing",
            branch="feature/x",
            delete_branch=False,
        )
    )

    assert result.status == "failed"
    assert result.error == "worktree path missing"


def test_build_connect_headers_ambient_factory_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.runner._entry as entry_mod

    monkeypatch.setattr(
        entry_mod,
        "_make_auth_token_factory",
        lambda *, server_url=None: (lambda: "ambient-jwt"),
    )
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert headers["Authorization"] == "Bearer ambient-jwt"


def test_build_connect_headers_swallows_factory_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runner._entry as entry_mod

    def _boom(*, server_url: str | None = None) -> object:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", _boom)
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


def test_build_connect_headers_incomplete_static_auth_username_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


def test_build_connect_headers_static_auth_login_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_url: str, **kwargs: object) -> _JsonResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "secret")
    monkeypatch.setattr("omnigent.host.connect.httpx.post", _boom)
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


def test_build_connect_headers_static_auth_login_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "secret")
    monkeypatch.setattr(
        "omnigent.host.connect.httpx.post",
        lambda *_a, **_k: _JsonResponse(200, ValueError("not json")),
    )
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


def test_build_connect_headers_static_auth_login_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "secret")
    monkeypatch.setattr(
        "omnigent.host.connect.httpx.post",
        lambda *_a, **_k: _JsonResponse(200, ["not", "a", "dict"]),
    )
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


def test_build_connect_headers_static_auth_login_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "secret")
    monkeypatch.setattr(
        "omnigent.host.connect.httpx.post",
        lambda *_a, **_k: _JsonResponse(200, {"token": ""}),
    )
    headers = _host("https://omnigent.example.com")._build_connect_headers()
    assert "Authorization" not in headers


class _DisconnectOnSecondRecvTunnel:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._recv_calls = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        self._recv_calls += 1
        raise ConnectionError("disconnect")


class _MessageThenDisconnectTunnel:
    def __init__(self, message: str) -> None:
        self.sent: list[str] = []
        self._message = message
        self._recv_calls = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        self._recv_calls += 1
        if self._recv_calls == 1:
            return self._message
        raise ConnectionError("disconnect")


@pytest.mark.asyncio
async def test_serve_frames_dispatches_inbound_host_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = _host()
    target = tmp_path / "file.txt"
    target.write_text("payload")
    frame = encode_host_frame(HostStatFrame(request_id="req_loop", path=str(target)))
    tunnel = _MessageThenDisconnectTunnel(frame)
    monkeypatch.setattr(
        "omnigent.host.connect.configured_harness_map",
        lambda: {"claude-sdk": True},
    )

    with pytest.raises(ConnectionError, match="disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type]

    assert len(tunnel.sent) >= 2
    result = decode_host_frame(tunnel.sent[-1])
    assert isinstance(result, HostStatResultFrame)
    assert result.request_id == "req_loop"


@pytest.mark.asyncio
async def test_serve_frames_ignores_recv_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    tunnel = _DisconnectOnSecondRecvTunnel()
    monkeypatch.setattr(
        "omnigent.host.connect.configured_harness_map",
        lambda: {"claude-sdk": True},
    )
    wait_calls = 0

    async def _short_wait_for(aw: object, timeout: float) -> object:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError()
        return await aw  # type: ignore[misc]

    monkeypatch.setattr("omnigent.host.connect.asyncio.wait_for", _short_wait_for)

    with pytest.raises(ConnectionError, match="disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type]

    assert wait_calls >= 2
    assert tunnel._recv_calls == 1
    assert len(tunnel.sent) >= 1


@pytest.mark.asyncio
async def test_handle_raw_message_replies_to_runner_ping() -> None:
    host = _host()
    tunnel = _SendOnceTunnel()
    ping = encode_keepalive_frame(PingFrame(ts=1700000000))

    await host._handle_raw_message(tunnel, ping)  # type: ignore[arg-type]

    assert len(tunnel.sent) == 1
    pong = decode_keepalive_frame(tunnel.sent[0])
    assert isinstance(pong, PongFrame)
    assert pong.ts == 1700000000


@pytest.mark.asyncio
async def test_handle_raw_message_replies_to_host_stat_frame(tmp_path: Path) -> None:
    host = _host()
    tunnel = _SendOnceTunnel()
    target = tmp_path / "file.txt"
    target.write_text("hello")
    frame = encode_host_frame(HostStatFrame(request_id="req_stat", path=str(target)))

    await host._handle_raw_message(tunnel, frame)  # type: ignore[arg-type]

    assert len(tunnel.sent) == 1
    result = decode_host_frame(tunnel.sent[0])
    assert isinstance(result, HostStatResultFrame)
    assert result.status == "ok"
    assert result.exists is True
    assert result.type == "file"


@pytest.mark.asyncio
async def test_handle_raw_message_ignores_unknown_payload() -> None:
    host = _host()
    tunnel = _SendOnceTunnel()
    await host._handle_raw_message(tunnel, '{"kind":"unknown"}')  # type: ignore[arg-type]
    assert tunnel.sent == []


@pytest.mark.asyncio
async def test_dispatch_host_frame_routes_worktree_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host()
    tunnel = _SendOnceTunnel()
    created = CreatedWorktree(worktree_path="/wt/x", branch="feature/x")

    async def _fake_to_thread(fn, **kwargs: object) -> object:
        if fn.__name__ == "create_worktree":
            return created
        return None

    monkeypatch.setattr("omnigent.host.connect.asyncio.to_thread", _fake_to_thread)

    await host._dispatch_host_frame(
        tunnel,  # type: ignore[arg-type]
        HostCreateWorktreeFrame(
            request_id="req_wt",
            repo_path="/repo",
            branch_name="feature/x",
        ),
    )
    await host._dispatch_host_frame(
        tunnel,  # type: ignore[arg-type]
        HostRemoveWorktreeFrame(
            request_id="req_rm",
            worktree_path="/wt/x",
            branch="feature/x",
            delete_branch=True,
        ),
    )

    assert len(tunnel.sent) == 2
    create_result = decode_host_frame(tunnel.sent[0])
    remove_result = decode_host_frame(tunnel.sent[1])
    assert isinstance(create_result, HostCreateWorktreeResultFrame)
    assert create_result.status == "ok"
    assert isinstance(remove_result, HostRemoveWorktreeResultFrame)
    assert remove_result.status == "ok"


def test_cleanup_runners_kills_on_wait_timeout(tmp_path: Path) -> None:
    host = _host()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["sleep"], timeout=5.0)
    host._runners["runner_z"] = SimpleNamespace(proc=proc, log_path=tmp_path / "z.log")

    host._cleanup_runners()

    proc.kill.assert_called_once()
    assert host._runners == {}


def test_run_host_process_announces_auto_generated_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("OMNIGENT_HOST_ID", "host_managed_test")
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "managed-host")
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))

    run_host_process(server_url="https://app.example.com", config_path=config_path)

    out = capsys.readouterr().out
    assert not config_path.exists()
    assert f"Auto-generated {config_path}" in out


def test_run_host_process_announces_cli_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_log = tmp_path / "cli-host.log"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))
    monkeypatch.setattr(
        "omnigent.cli_diagnostics.current_cli_log_path",
        lambda: cli_log,
    )

    run_host_process(
        server_url="https://app.example.com",
        config_path=tmp_path / "config.yaml",
    )

    out = capsys.readouterr().out
    assert "This host's log: ~/cli-host.log" in out


@pytest.mark.asyncio
async def test_run_resets_backoff_after_successful_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host("https://app.example.databricks.com")
    calls = 0

    async def _connect_script() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        raise asyncio.CancelledError()

    monkeypatch.setattr(host, "_connect_and_serve", _connect_script)
    monkeypatch.setattr(host, "_cleanup_runners", lambda: None)

    await host.run()
    assert calls == 2


@pytest.mark.asyncio
async def test_run_swallows_cancelled_error_during_reconnect_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.01)
    host = _host("https://app.example.databricks.com")

    async def _boom() -> None:
        raise ConnectionError("no close frame")

    monkeypatch.setattr(host, "_connect_and_serve", _boom)
    monkeypatch.setattr(host, "_cleanup_runners", lambda: None)

    task = asyncio.create_task(host.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await task


@pytest.mark.asyncio
async def test_watch_runner_returns_when_handle_replaced_before_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _host()
    exited = asyncio.Event()
    proc = MagicMock()
    proc.poll.side_effect = lambda: 0 if exited.is_set() else None
    original = SimpleNamespace(proc=proc, log_path=tmp_path / "r.log")
    replaced = SimpleNamespace(proc=proc, log_path=tmp_path / "other.log")
    host._runners["runner_x"] = original

    with patch.object(host, "_report_runner_exit", autospec=True) as report:
        task = asyncio.create_task(host._watch_runner("runner_x"))
        await asyncio.sleep(0.02)
        host._runners["runner_x"] = replaced
        exited.set()
        await asyncio.wait_for(task, timeout=1.0)

    report.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_host_frame_routes_launch_stop_and_list_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host()
    tunnel = _SendOnceTunnel()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "alpha.txt").write_text("a")

    async def _fake_launch(frame: HostLaunchRunnerFrame) -> object:
        from omnigent.host.frames import HostLaunchRunnerResultFrame

        return HostLaunchRunnerResultFrame(
            request_id=frame.request_id,
            status="failed",
            error="spawn skipped in test",
        )

    monkeypatch.setattr(host, "_handle_launch", _fake_launch)

    await host._dispatch_host_frame(
        tunnel,  # type: ignore[arg-type]
        HostLaunchRunnerFrame(
            request_id="req_launch",
            binding_token="tok",
            workspace=str(workspace),
        ),
    )
    await host._dispatch_host_frame(
        tunnel,  # type: ignore[arg-type]
        HostStopRunnerFrame(request_id="req_stop", runner_id="missing"),
    )
    await host._dispatch_host_frame(
        tunnel,  # type: ignore[arg-type]
        HostListDirFrame(request_id="req_list", path=str(workspace)),
    )

    assert len(tunnel.sent) == 3
