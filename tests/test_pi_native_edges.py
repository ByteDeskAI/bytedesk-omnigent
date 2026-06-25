"""Edge tests for omnigent.pi_native launch and terminal helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import click
import httpx
import pytest

from omnigent import pi_native
from omnigent._wrapper_labels import PI_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY


def test_resolve_pi_executable_uses_env_override() -> None:
    env = {"OMNIGENT_PI_PATH": "/opt/pi"}
    assert pi_native.resolve_pi_executable(env=env, which=lambda _c: "/opt/pi") == "/opt/pi"


def test_resolve_pi_executable_honors_legacy_harness_env() -> None:
    env = {"HARNESS_PI_PATH": "/legacy/pi"}
    assert pi_native.resolve_pi_executable(env=env, which=lambda _c: "/legacy/pi") == "/legacy/pi"


def test_resolve_pi_executable_raises_when_missing() -> None:
    with pytest.raises(click.ClickException, match="pi"):
        pi_native.resolve_pi_executable(env={}, which=lambda _c: None)


def test_build_pi_launch_includes_pass_through_args() -> None:
    launch = pi_native.build_pi_launch(["--model", "gpt"], env={}, which=lambda _c: "/bin/pi")
    assert launch.executable == "/bin/pi"
    assert launch.argv == ["/bin/pi", "--model", "gpt"]


def test_materialize_pi_agent_spec_writes_yaml(tmp_path: Path) -> None:
    path = pi_native._materialize_pi_agent_spec(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "pi-native-ui" in text
    assert "pi-native" in text


def test_run_pi_native_requires_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: "/usr/bin/tmux")
    with pytest.raises(click.ClickException, match="server URL"):
        pi_native.run_pi_native(server=None, session_id=None, pi_args=())


def test_preflight_raises_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pi_native.shutil, "which", lambda c: None if c == "tmux" else f"/usr/bin/{c}"
    )
    with pytest.raises(click.ClickException, match="tmux"):
        pi_native._preflight_local_tools()


def test_launched_pi_terminal_from_payload_decodes_metadata() -> None:
    terminal = pi_native._launched_pi_terminal_from_payload(
        {
            "id": "term_pi",
            "metadata": {"tmux_socket": "/tmp/pi.sock", "tmux_target": "main", "running": True},
        }
    )
    assert terminal.terminal_id == "term_pi"
    assert terminal.tmux_socket == Path("/tmp/pi.sock")
    assert terminal.tmux_target == "main"


def test_launched_pi_terminal_from_payload_rejects_bad_shape() -> None:
    with pytest.raises(click.ClickException, match="non-object"):
        pi_native._launched_pi_terminal_from_payload("bad")
    with pytest.raises(click.ClickException, match="terminal id"):
        pi_native._launched_pi_terminal_from_payload({"id": ""})


def test_direct_tmux_unavailable_reason_reports_missing_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=None,
        tmux_target="main",
        reattached=False,
    )
    assert "tmux socket" in (pi_native._direct_tmux_unavailable_reason(prepared) or "")

    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=tmp_path / "missing.sock",
        tmux_target=None,
        reattached=False,
    )
    assert "tmux target" in (pi_native._direct_tmux_unavailable_reason(prepared) or "")

    sock = tmp_path / "gone.sock"
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=sock,
        tmux_target="main",
        reattached=False,
    )
    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: "/usr/bin/tmux")
    assert "not reachable" in (pi_native._direct_tmux_unavailable_reason(prepared) or "")


def test_direct_tmux_unavailable_reason_reports_missing_tmux_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = tmp_path / "pi.sock"
    sock.write_text("", encoding="utf-8")
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=sock,
        tmux_target="main",
        reattached=False,
    )
    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: None)
    assert "tmux is not available" in (pi_native._direct_tmux_unavailable_reason(prepared) or "")


def test_update_startup_progress_noops_without_renderer() -> None:
    pi_native._update_startup_progress(None, "ignored")


@pytest.mark.asyncio
async def test_fetch_pi_session_maps_404_to_click_exception() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="not found"):
            await pi_native._fetch_pi_session(client, "conv_missing")


@pytest.mark.asyncio
async def test_fetch_pi_session_returns_payload() -> None:
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, json={"session_id": "conv_1", "labels": {}})
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = await pi_native._fetch_pi_session(client, "conv_1")
    assert payload["session_id"] == "conv_1"


@pytest.mark.asyncio
async def test_create_pi_session_returns_session_id() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(201, json={"session_id": "conv_new"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sid = await pi_native._create_pi_session(client, b"bundle", terminal_launch_args=["--x"])
    assert sid == "conv_new"


@pytest.mark.asyncio
async def test_create_pi_session_surfaces_http_errors() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="creation failed"):
            await pi_native._create_pi_session(client, b"bundle")


@pytest.mark.asyncio
async def test_find_running_pi_terminal_returns_none_on_404() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert await pi_native._find_running_pi_terminal(client, "conv_1") is None


@pytest.mark.asyncio
async def test_find_running_pi_terminal_treats_offline_as_absent() -> None:
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(503, text="runner offline for session")
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert await pi_native._find_running_pi_terminal(client, "conv_1") is None


@pytest.mark.asyncio
async def test_find_running_pi_terminal_decodes_running_terminal() -> None:
    terminal_id = pi_native.pi_terminal_resource_id()

    def _handler(request: httpx.Request) -> httpx.Response:
        assert terminal_id in request.url.path
        return httpx.Response(
            200,
            json={
                "id": terminal_id,
                "metadata": {"tmux_socket": "/tmp/pi.sock", "tmux_target": "main"},
            },
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        terminal = await pi_native._find_running_pi_terminal(client, "conv_1")
    assert terminal is not None
    assert terminal.tmux_target == "main"


@pytest.mark.asyncio
async def test_wait_for_pi_terminal_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(404))
    monkeypatch.setattr(pi_native.asyncio, "sleep", _async_noop)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="did not create"):
            await pi_native._wait_for_pi_terminal_ready(client, "conv_1", timeout_s=0.0)


@pytest.mark.asyncio
async def test_prepare_pi_terminal_reattaches_existing_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = pi_native.LaunchedPiTerminal(
        terminal_id="term_existing",
        tmux_socket=Path("/tmp/pi.sock"),
        tmux_target="main",
    )

    async def _fake_fetch(_client: httpx.AsyncClient, _sid: str) -> dict[str, Any]:
        return {"labels": {"omnigent.wrapper": PI_NATIVE_WRAPPER_VALUE}}

    async def _fake_find(_client: httpx.AsyncClient, _sid: str) -> pi_native.LaunchedPiTerminal:
        return existing

    monkeypatch.setattr(pi_native, "_fetch_pi_session", _fake_fetch)
    monkeypatch.setattr(pi_native, "_find_running_pi_terminal", _fake_find)
    prepared = await pi_native._prepare_pi_terminal_via_daemon(
        base_url="http://test",
        headers={},
        session_id="conv_resume",
        session_bundle=None,
        pi_args=("--model", "x"),
        host_id="host-1",
        workspace="/tmp/ws",
    )
    assert prepared.reattached is True
    assert prepared.terminal_id == "term_existing"
    assert "Ignoring Pi launch args" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_prepare_pi_terminal_rejects_non_pi_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(_client: httpx.AsyncClient, _sid: str) -> dict[str, Any]:
        return {"labels": {"omnigent.wrapper": "other"}}

    monkeypatch.setattr(pi_native, "_fetch_pi_session", _fake_fetch)
    with pytest.raises(click.ClickException, match="not a pi-native"):
        await pi_native._prepare_pi_terminal_via_daemon(
            base_url="http://test",
            headers={},
            session_id="conv_bad",
            session_bundle=None,
            pi_args=(),
            host_id="host-1",
            workspace="/tmp/ws",
        )


@pytest.mark.asyncio
async def test_attach_terminal_resource_requires_tmux_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = tmp_path / "pi.sock"
    sock.write_text("", encoding="utf-8")
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=sock,
        tmux_target="main",
        reattached=False,
    )
    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: "/usr/bin/tmux")

    async def _fake_attach(_socket: Path, _target: str) -> None:
        return None

    monkeypatch.setattr(pi_native, "_attach_direct_tmux", _fake_attach)
    await pi_native._attach_terminal_resource(prepared)


def test_resolve_session_id_for_resume_returns_explicit_id() -> None:
    sid = pi_native._resolve_session_id_for_resume(
        base_url="http://test",
        headers={},
        session_id="conv_explicit",
        resume_picker=False,
    )
    assert sid == "conv_explicit"


def test_resolve_session_id_for_resume_skips_picker_when_disabled() -> None:
    assert (
        pi_native._resolve_session_id_for_resume(
            base_url="http://test",
            headers={},
            session_id=None,
            resume_picker=False,
        )
        is None
    )


def test_run_with_remote_server_returns_early_when_picker_cancels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec_path = pi_native._materialize_pi_agent_spec(tmp_path)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda server_url: {"Authorization": "Bearer t"},
    )
    monkeypatch.setattr(
        pi_native,
        "_resolve_session_id_for_resume",
        lambda **_kw: None,
    )
    pi_native._run_with_remote_server(
        "http://127.0.0.1:8787",
        spec_path,
        session_id=None,
        resume_picker=True,
        pi_args=(),
    )


def test_run_with_remote_server_maps_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec_path = pi_native._materialize_pi_agent_spec(tmp_path)

    def _boom(**_kw: object) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("omnigent.chat._remote_headers", lambda server_url: {})
    monkeypatch.setattr(pi_native, "_resolve_session_id_for_resume", _boom)
    with pytest.raises(click.ClickException, match="Could not reach"):
        pi_native._run_with_remote_server(
            "http://127.0.0.1:8787",
            spec_path,
            session_id="conv_1",
            resume_picker=False,
            pi_args=(),
        )


@pytest.mark.asyncio
async def test_prepare_pi_terminal_requires_bundle_for_fresh_session() -> None:
    with pytest.raises(click.ClickException, match="session bundle"):
        await pi_native._prepare_pi_terminal_via_daemon(
            base_url="http://test",
            headers={},
            session_id=None,
            session_bundle=None,
            pi_args=(),
            host_id="host-1",
            workspace="/tmp/ws",
        )


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_run_pi_native_materializes_spec_and_calls_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, Path]] = []

    def _capture(
        base_url: str,
        spec_path: Path,
        *,
        session_id: str | None,
        resume_picker: bool,
        pi_args: tuple[str, ...],
        auto_open_conversation: bool = False,
    ) -> None:
        seen.append((base_url, spec_path))

    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: "/usr/bin/tmux")
    monkeypatch.setattr(pi_native, "_run_with_remote_server", _capture)
    pi_native.run_pi_native(
        server="http://127.0.0.1:8787/",
        session_id="conv_1",
        pi_args=("--model", "x"),
    )
    assert len(seen) == 1
    assert seen[0][0] == "http://127.0.0.1:8787"
    assert seen[0][1].name == "pi-native-ui.yaml"


def test_update_startup_progress_forwards_to_renderer() -> None:
    class _Progress:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def update(self, message: str) -> None:
            self.messages.append(message)

    progress = _Progress()
    pi_native._update_startup_progress(progress, "Starting Pi terminal...")
    assert progress.messages == ["Starting Pi terminal..."]


def test_pi_terminal_resource_id_and_bridge_dir() -> None:
    terminal_id = pi_native.pi_terminal_resource_id()
    assert terminal_id
    bridge = pi_native.pi_bridge_dir_for_session("conv_pi")
    assert "pi-native" in str(bridge)


@pytest.mark.asyncio
async def test_create_pi_session_rejects_missing_session_id() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(201, json={}))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="session_id"):
            await pi_native._create_pi_session(client, b"bundle")


@pytest.mark.asyncio
async def test_fetch_pi_session_surfaces_http_errors() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="Failed to fetch"):
            await pi_native._fetch_pi_session(client, "conv_1")


@pytest.mark.asyncio
async def test_fetch_pi_session_rejects_non_object_payload() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=["bad"]))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="non-object"):
            await pi_native._fetch_pi_session(client, "conv_1")


@pytest.mark.asyncio
async def test_ensure_pi_terminal_on_runner_raises_on_failure() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="fail"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="ensure failed"):
            await pi_native._ensure_pi_terminal_on_runner(client, "conv_1")


@pytest.mark.asyncio
async def test_wait_for_pi_terminal_ready_returns_when_terminal_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = pi_native.LaunchedPiTerminal(
        terminal_id="term_ready",
        tmux_socket=Path("/tmp/pi.sock"),
        tmux_target="main",
    )
    monkeypatch.setattr(
        pi_native,
        "_find_running_pi_terminal",
        AsyncMock(side_effect=[None, terminal]),
    )
    monkeypatch.setattr(pi_native.asyncio, "sleep", _async_noop)
    transport = httpx.MockTransport(lambda _req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await pi_native._wait_for_pi_terminal_ready(client, "conv_1", timeout_s=1.0)
    assert ready.terminal_id == "term_ready"


@pytest.mark.asyncio
async def test_find_running_pi_terminal_raises_on_unexpected_http_error() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="kaboom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="Failed to fetch Pi terminal"):
            await pi_native._find_running_pi_terminal(client, "conv_1")


@pytest.mark.asyncio
async def test_find_running_pi_terminal_treats_stopped_terminal_as_absent() -> None:
    terminal_id = pi_native.pi_terminal_resource_id()

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": terminal_id, "metadata": {"running": False}},
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert await pi_native._find_running_pi_terminal(client, "conv_1") is None


@pytest.mark.asyncio
async def test_attach_terminal_resource_raises_when_tmux_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=tmp_path / "missing.sock",
        tmux_target="main",
        reattached=False,
    )
    monkeypatch.setattr(pi_native.shutil, "which", lambda _c: "/usr/bin/tmux")
    with pytest.raises(click.ClickException, match="requires direct tmux attach"):
        await pi_native._attach_terminal_resource(prepared)


@pytest.mark.asyncio
async def test_attach_terminal_resource_raises_on_incomplete_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=None,
        tmux_target=None,
        reattached=False,
    )
    monkeypatch.setattr(pi_native, "_direct_tmux_unavailable_reason", lambda _p: None)
    with pytest.raises(click.ClickException, match="incomplete"):
        await pi_native._attach_terminal_resource(prepared)


@pytest.mark.asyncio
async def test_attach_direct_tmux_invokes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = tmp_path / "pi.sock"
    sock.write_text("", encoding="utf-8")

    class _Proc:
        async def wait(self) -> int:
            return 0

    seen: list[list[str]] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> _Proc:
        seen.append(list(argv))
        return _Proc()

    monkeypatch.setattr(pi_native.asyncio, "create_subprocess_exec", _fake_exec)
    await pi_native._attach_direct_tmux(sock, "main")
    assert seen[0][:3] == ["tmux", "-S", str(sock)]


@pytest.mark.asyncio
async def test_prepare_pi_terminal_creates_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = pi_native.LaunchedPiTerminal(
        terminal_id="term_new",
        tmux_socket=Path("/tmp/pi.sock"),
        tmux_target="main",
    )
    progress = SimpleNamespace(messages=[], update=lambda m: progress.messages.append(m))
    monkeypatch.setattr(pi_native, "_create_pi_session", AsyncMock(return_value="conv_new"))
    monkeypatch.setattr(pi_native, "wait_for_host_online", AsyncMock())
    monkeypatch.setattr(
        pi_native, "launch_or_reuse_daemon_runner", AsyncMock(return_value="runner_1")
    )
    monkeypatch.setattr(pi_native, "wait_for_runner_online", AsyncMock())
    monkeypatch.setattr(pi_native, "_bind_session_runner", AsyncMock())
    monkeypatch.setattr(pi_native, "_ensure_pi_terminal_on_runner", AsyncMock())
    monkeypatch.setattr(pi_native, "_wait_for_pi_terminal_ready", AsyncMock(return_value=terminal))
    prepared = await pi_native._prepare_pi_terminal_via_daemon(
        base_url="http://test",
        headers={},
        session_id=None,
        session_bundle=b"bundle",
        pi_args=("--model", "x"),
        host_id="host-1",
        workspace="/tmp/ws",
        startup_progress=progress,  # type: ignore[arg-type]
    )
    assert prepared.session_id == "conv_new"
    assert prepared.reattached is False
    assert any("Creating Pi session" in message for message in progress.messages)


@pytest.mark.asyncio
async def test_prepare_pi_terminal_patches_launch_args_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = pi_native.LaunchedPiTerminal(
        terminal_id="term_patch",
        tmux_socket=Path("/tmp/pi.sock"),
        tmux_target="main",
    )
    patched: list[str] = []

    async def _fake_fetch(_client: httpx.AsyncClient, _sid: str) -> dict[str, Any]:
        return {"labels": {WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE}}

    async def _fake_find(_client: httpx.AsyncClient, _sid: str) -> None:
        return None

    class _Client:
        async def patch(
            self, url: str, json: object | None = None, **kwargs: object
        ) -> httpx.Response:
            patched.append(url)
            return httpx.Response(200, json={})

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(pi_native, "_fetch_pi_session", _fake_fetch)
    monkeypatch.setattr(pi_native, "_find_running_pi_terminal", _fake_find)
    monkeypatch.setattr(pi_native, "wait_for_host_online", AsyncMock())
    monkeypatch.setattr(
        pi_native, "launch_or_reuse_daemon_runner", AsyncMock(return_value="runner_1")
    )
    monkeypatch.setattr(pi_native, "wait_for_runner_online", AsyncMock())
    monkeypatch.setattr(pi_native, "_bind_session_runner", AsyncMock())
    monkeypatch.setattr(pi_native, "_ensure_pi_terminal_on_runner", AsyncMock())
    monkeypatch.setattr(pi_native, "_wait_for_pi_terminal_ready", AsyncMock(return_value=terminal))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())
    prepared = await pi_native._prepare_pi_terminal_via_daemon(
        base_url="http://test",
        headers={},
        session_id="conv_resume",
        session_bundle=None,
        pi_args=("--model", "y"),
        host_id="host-1",
        workspace="/tmp/ws",
    )
    assert prepared.session_id == "conv_resume"
    assert patched


@pytest.mark.asyncio
async def test_prepare_pi_terminal_patch_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def patch(
            self, url: str, json: object | None = None, **kwargs: object
        ) -> httpx.Response:
            return httpx.Response(500, text="patch failed")

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    async def _fake_fetch(_client: httpx.AsyncClient, _sid: str) -> dict[str, Any]:
        return {"labels": {WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE}}

    async def _fake_find(_client: httpx.AsyncClient, _sid: str) -> None:
        return None

    monkeypatch.setattr(pi_native, "_fetch_pi_session", _fake_fetch)
    monkeypatch.setattr(pi_native, "_find_running_pi_terminal", _fake_find)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())
    with pytest.raises(click.ClickException, match="launch config update failed"):
        await pi_native._prepare_pi_terminal_via_daemon(
            base_url="http://test",
            headers={},
            session_id="conv_resume",
            session_bundle=None,
            pi_args=("--model", "y"),
            host_id="host-1",
            workspace="/tmp/ws",
        )


def test_run_with_remote_server_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = pi_native._materialize_pi_agent_spec(tmp_path)
    prepared = pi_native.PreparedPiTerminal(
        session_id="conv_drive",
        terminal_id="term_drive",
        tmux_socket=Path("/tmp/pi.sock"),
        tmux_target="main",
        reattached=False,
    )

    class _Progress:
        def update(self, _message: str) -> None:
            return None

        def __enter__(self) -> _Progress:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    async def _fake_prepare(**_kw: object) -> pi_native.PreparedPiTerminal:
        return prepared

    async def _fake_attach(_prepared: pi_native.PreparedPiTerminal) -> None:
        return None

    monkeypatch.setattr(pi_native, "runner_startup_progress", lambda **_kw: _Progress())
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **_kw: {})
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda _p: b"bundle")
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host-1"),
    )
    monkeypatch.setattr(
        pi_native,
        "_resolve_session_id_for_resume",
        lambda **_kw: None,
    )
    monkeypatch.setattr(pi_native, "_prepare_pi_terminal_via_daemon", _fake_prepare)
    monkeypatch.setattr(pi_native, "_attach_terminal_resource", _fake_attach)
    monkeypatch.setattr(pi_native, "open_conversation_link_if_enabled", lambda **_kw: None)
    monkeypatch.setattr(pi_native, "echo_native_resume_hint", lambda **_kw: None)
    pi_native._run_with_remote_server(
        "http://127.0.0.1:8787",
        spec_path,
        session_id=None,
        resume_picker=False,
        pi_args=(),
        auto_open_conversation=False,
    )
    assert "conv_drive" in capsys.readouterr().err


def test_resolve_session_id_for_resume_uses_picker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_pick(_client: object, **_kw: object) -> str:
        return "conv_picked"

    class _FakeClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr("omnigent_client.OmnigentClient", _FakeClient)
    monkeypatch.setattr(
        "omnigent.repl._resume_picker.pick_conversation_by_wrapper_label_from_sdk",
        _fake_pick,
    )
    sid = pi_native._resolve_session_id_for_resume(
        base_url="http://test",
        headers={},
        session_id=None,
        resume_picker=True,
    )
    assert sid == "conv_picked"
