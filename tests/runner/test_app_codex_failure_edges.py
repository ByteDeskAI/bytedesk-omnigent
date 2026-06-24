"""Failure-path coverage for codex-native helpers in :mod:`omnigent.runner.app`."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner.app import (
    _AUTO_CODEX_APP_SERVERS,
    _CodexNativeLaunchConfig,
    _auto_create_codex_terminal,
    _codex_discover_thread_and_forward,
    _codex_ensure_response_with_policy_notice,
    _codex_forward_known_thread,
    _ensure_orchestrator_skills_in_bundle,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
def _resume_launch_config(
    tmp_path: Path,
    *,
    external_session_id: str = "019e96aa-0be2-7343-8d3b-6f914d60936b",
    fork_source_id: str | None = None,
    fork_source_external_id: str | None = None,
    fork_carry_history: bool = False,
) -> _CodexNativeLaunchConfig:
    return _CodexNativeLaunchConfig(
        workspace=tmp_path / "workspace",
        policy_server_url="http://127.0.0.1:8000",
        terminal_launch_args=None,
        model_override=None,
        external_session_id=external_session_id,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
    )


def _fresh_launch_config(tmp_path: Path) -> _CodexNativeLaunchConfig:
    return _CodexNativeLaunchConfig(
        workspace=tmp_path / "workspace",
        policy_server_url="http://127.0.0.1:8000",
        terminal_launch_args=None,
        model_override=None,
        external_session_id=None,
        fork_source_id=None,
        fork_source_external_id=None,
        fork_carry_history=False,
    )


class _FakeCodexAppServer:
    codex_path = "/opt/codex/bin/codex"

    def __init__(self, tmp_path: Path, session_id: str) -> None:
        from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

        self.env = {"OPENAI_API_KEY": "sk-test"}
        self.codex_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
        self.listen_url: str | None = None
        self.started = False
        self.config_overrides: list[str] = []
        self.policy_notice_pending = False
        self.policy_hook_disabled_reason: str | None = None
        self._tmp_path = tmp_path

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        pass


@pytest.fixture
def codex_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import omnigent.codex_native_bridge as codex_native_bridge

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    return workspace


def _install_fake_codex_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str,
    *,
    discovery_connect_raises: Exception | None = None,
) -> _FakeCodexAppServer:
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod

    app_server = _FakeCodexAppServer(tmp_path, session_id)

    def _fake_build(**kwargs: Any) -> _FakeCodexAppServer:
        del kwargs
        return app_server

    class _DiscoveryClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            self.ws_url = ws_url
            self.client_name = client_name
            self.closed = False

        async def connect(self) -> None:
            if discovery_connect_raises is not None:
                raise discovery_connect_raises

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(codex_app_mod, "build_codex_native_server", _fake_build)
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _DiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", AsyncMock())
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", AsyncMock())
    monkeypatch.setattr(runner_app_mod, "_codex_discover_thread_and_forward", AsyncMock())
    return app_server


class _LaunchRegistry:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.launched: list[Any] = []

    async def launch_auxiliary_terminal(self, **kwargs: Any) -> SessionResourceView:
        self.launched.append(kwargs)
        if self.fail:
            raise RuntimeError("tmux launch failed")
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=kwargs["session_id"],
            name="Codex",
        )


@pytest.mark.asyncio
async def test_auto_create_codex_clone_rollout_failure_launches_fresh(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native as codex_native
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_clone_fail"
    source_id = "conv_codex_source"
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(
            return_value=_CodexNativeLaunchConfig(
                workspace=codex_env,
                policy_server_url="http://127.0.0.1:8000",
                terminal_launch_args=None,
                model_override=None,
                external_session_id=None,
                fork_source_id=source_id,
                fork_source_external_id=source_thread,
                fork_carry_history=False,
            )
        ),
    )

    def _boom(**_kwargs: Any) -> Path:
        raise OSError("clone failed")

    monkeypatch.setattr(codex_native, "_clone_codex_rollout", _boom)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        view = await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(patch=AsyncMock()),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

    assert view.id == "terminal_codex_main"
    assert "Could not clone source rollout" in caplog.text
    runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_auto_create_codex_fork_patch_failure_still_launches(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native as codex_native
    import omnigent.codex_native_bridge as codex_native_bridge
    import omnigent.runner.app as runner_app_mod
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    session_id = "conv_codex_patch_fail"
    source_id = "conv_codex_source"
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"

    bridge_dir = bridge_dir_for_bridge_id(session_id)
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(source_id))
    source_rollout_dir = source_home / "sessions" / "2026" / "06" / "05"
    source_rollout_dir.mkdir(parents=True)
    (source_rollout_dir / f"rollout-2026-06-05T15-23-07-{source_thread}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": source_thread, "cwd": "/old"}}) + "\n"
    )

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(
            return_value=_CodexNativeLaunchConfig(
                workspace=codex_env,
                policy_server_url="http://127.0.0.1:8000",
                terminal_launch_args=None,
                model_override=None,
                external_session_id=None,
                fork_source_id=source_id,
                fork_source_external_id=source_thread,
                fork_carry_history=False,
            )
        ),
    )

    async def _patch_fail(*_a: Any, **_k: Any) -> httpx.Response:
        raise httpx.ConnectError("ap down")

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        view = await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(patch=AsyncMock(side_effect=_patch_fail)),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

    assert view.id == "terminal_codex_main"
    assert "Could not pre-set external_session_id" in caplog.text
    cloned = list(
        codex_home_for_bridge_dir(bridge_dir).glob("sessions/**/rollout-*.jsonl")
    )
    assert cloned, "fork clone should still materialize rollout on disk"
    runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_auto_create_codex_fork_rebuild_patch_failure_still_launches(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native as codex_native
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_rebuild_patch"
    built = tmp_path / "built-rollout.jsonl"
    built.write_text("{}\n")

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(
            return_value=_CodexNativeLaunchConfig(
                workspace=codex_env,
                policy_server_url="http://127.0.0.1:8000",
                terminal_launch_args=None,
                model_override=None,
                external_session_id=None,
                fork_source_id="conv_sdk_source",
                fork_source_external_id=None,
                fork_carry_history=True,
            )
        ),
    )
    monkeypatch.setattr(
        codex_native,
        "_ensure_local_codex_resume_rollout",
        AsyncMock(return_value=built),
    )

    async def _patch_fail(*_a: Any, **_k: Any) -> httpx.Response:
        raise httpx.ConnectError("ap down")

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        view = await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(patch=AsyncMock(side_effect=_patch_fail)),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

    assert view.id == "terminal_codex_main"
    assert "Could not pre-set external_session_id" in caplog.text
    runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_auto_create_codex_fork_rebuild_failure_launches_fresh(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native as codex_native
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_rebuild_fail"

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(
            return_value=_CodexNativeLaunchConfig(
                workspace=codex_env,
                policy_server_url="http://127.0.0.1:8000",
                terminal_launch_args=None,
                model_override=None,
                external_session_id=None,
                fork_source_id="conv_sdk_source",
                fork_source_external_id=None,
                fork_carry_history=True,
            )
        ),
    )
    monkeypatch.setattr(
        codex_native,
        "_ensure_local_codex_resume_rollout",
        AsyncMock(side_effect=RuntimeError("items unavailable")),
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        view = await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(patch=AsyncMock()),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

    assert view.id == "terminal_codex_main"
    assert "Could not build rollout from items" in caplog.text
    runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_auto_create_codex_cold_resume_requires_server_client(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_cold_resume"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(return_value=_resume_launch_config(tmp_path, external_session_id=thread_id)),
    )

    with pytest.raises(RuntimeError, match="server_client is required for Codex cold resume"):
        await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=None,
        )


@pytest.mark.asyncio
async def test_auto_create_codex_populate_skills_oserror_continues(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_skills_fail"
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    _install_fake_codex_server(monkeypatch, tmp_path, session_id)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(return_value=_fresh_launch_config(tmp_path)),
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("skills link failed")

    monkeypatch.setattr(
        "omnigent.inner.codex_executor.populate_codex_skills_from_bundle",
        _boom,
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        view = await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            bundle_dir=bundle_dir,
            server_client=SimpleNamespace(get=AsyncMock()),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

    assert view.id == "terminal_codex_main"
    assert "Could not populate codex skills" in caplog.text
    runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_auto_create_codex_connect_failure_cleans_up(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_connect_fail"
    app_server = _install_fake_codex_server(
        monkeypatch,
        tmp_path,
        session_id,
        discovery_connect_raises=RuntimeError("ws handshake failed"),
    )
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(return_value=_fresh_launch_config(tmp_path)),
    )

    with pytest.raises(RuntimeError, match="ws handshake failed"):
        await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(get=AsyncMock()),  # type: ignore[arg-type]
        )

    assert session_id not in _AUTO_CODEX_APP_SERVERS
    assert app_server.started is True


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_launch_failure_cleans_up(
    codex_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_launch_fail"
    closed = {"client": False, "server": False}

    class _Client:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            closed["client"] = True

    app_server = _FakeCodexAppServer(tmp_path, session_id)

    async def _close() -> None:
        closed["server"] = True

    app_server.close = _close  # type: ignore[method-assign]
    monkeypatch.setattr(codex_app_mod, "build_codex_native_server", lambda **_k: app_server)
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _Client)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_native_launch_config",
        AsyncMock(return_value=_fresh_launch_config(tmp_path)),
    )

    with pytest.raises(RuntimeError, match="tmux launch failed"):
        await _auto_create_codex_terminal(
            session_id,
            _LaunchRegistry(fail=True),  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=SimpleNamespace(get=AsyncMock()),  # type: ignore[arg-type]
        )

    assert closed["client"] is True
    assert closed["server"] is True
    assert session_id not in _AUTO_CODEX_APP_SERVERS


@pytest.mark.asyncio
async def test_codex_discover_thread_and_forward_patch_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native_forwarder as codex_native_forwarder

    session_id = "conv_codex_patch_ext"
    thread_id = "019e96aa-1111-7222-8333-444455556666"
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    class _Client:
        async def close(self) -> None:
            pass

    class _AppServer:
        async def close(self) -> None:
            pass

    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()

    monkeypatch.setattr(
        codex_native_forwarder,
        "wait_for_thread_started",
        AsyncMock(return_value=thread_id),
    )
    monkeypatch.setattr(codex_native_forwarder, "supervise_forwarder", AsyncMock())
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _PatchClient:
        async def patch(self, *_a: Any, **_k: Any) -> httpx.Response:
            raise httpx.ConnectError("ap down")

        async def __aenter__(self) -> _PatchClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("omnigent.runner.app.httpx.AsyncClient", lambda **_k: _PatchClient())

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )

    assert "Could not record codex external_session_id" in caplog.text
    assert session_id not in _AUTO_CODEX_APP_SERVERS


@pytest.mark.asyncio
async def test_codex_discover_thread_and_forward_patch_rejected_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.codex_native_forwarder as codex_native_forwarder

    session_id = "conv_codex_patch_400"
    thread_id = "019e96aa-2222-7222-8333-444455556666"
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    class _Client:
        async def close(self) -> None:
            pass

    _AUTO_CODEX_APP_SERVERS[session_id] = SimpleNamespace(close=AsyncMock())

    monkeypatch.setattr(
        codex_native_forwarder,
        "wait_for_thread_started",
        AsyncMock(return_value=thread_id),
    )
    monkeypatch.setattr(codex_native_forwarder, "supervise_forwarder", AsyncMock())
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _PatchClient:
        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            return httpx.Response(400, request=httpx.Request("PATCH", url))

        async def __aenter__(self) -> _PatchClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("omnigent.runner.app.httpx.AsyncClient", lambda **_k: _PatchClient())

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )

    assert "AP rejected codex external_session_id PATCH (400)" in caplog.text


@pytest.mark.asyncio
async def test_codex_forward_known_thread_cleans_up_app_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omnigent.codex_native_forwarder as codex_native_forwarder

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    session_id = "conv_codex_forward_known"
    closed = {"server": False}

    class _AppServer:
        async def close(self) -> None:
            closed["server"] = True

    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(codex_native_forwarder, "supervise_forwarder", AsyncMock())

    await _codex_forward_known_thread(
        session_id=session_id,
        bridge_dir=tmp_path,
        codex_ws_url="ws://127.0.0.1:1",
        thread_id="019e96aa-3333-7222-8333-444455556666",
    )

    assert closed["server"] is True
    assert session_id not in _AUTO_CODEX_APP_SERVERS


def test_codex_ensure_response_with_policy_notice(tmp_path: Path) -> None:
    session_id = "conv_codex_policy"
    view = SessionResourceView(
        id="terminal_codex_main",
        type="terminal",
        session_id=session_id,
        name="Codex",
        metadata={},
    )
    app_server = _FakeCodexAppServer(tmp_path, session_id)
    app_server.policy_notice_pending = True
    app_server.policy_hook_disabled_reason = "codex too old"
    _AUTO_CODEX_APP_SERVERS[session_id] = app_server
    try:
        response = _codex_ensure_response_with_policy_notice(session_id, view)
        body = json.loads(response.body)
        assert body["policy_hook_disabled_reason"] == "codex too old"
        assert app_server.policy_notice_pending is False
    finally:
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)


def test_ensure_orchestrator_skills_skips_when_canonical_source_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "no-source-bundle"
    bundle_dir.mkdir()
    original_is_dir = Path.is_dir

    def _is_dir(self: Path) -> bool:
        if "onboarding/agent/skills/build-omnigent" in self.as_posix():
            return False
        return original_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)
    _ensure_orchestrator_skills_in_bundle(bundle_dir, agent_spec=None)
    assert not (bundle_dir / "skills" / "build-omnigent").exists()


def test_ensure_orchestrator_skills_in_bundle_paths(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    bundle_dir = tmp_path / "bundle"
    skills_dir = bundle_dir / "skills" / "build-omnigent"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("exists")
    _ensure_orchestrator_skills_in_bundle(bundle_dir, agent_spec=None)

    fresh_bundle = tmp_path / "fresh-bundle"
    fresh_bundle.mkdir()
    source_skill = (
        Path(__file__).resolve().parents[2]
        / "omnigent"
        / "onboarding"
        / "agent"
        / "skills"
        / "build-omnigent"
    )
    if source_skill.is_dir():
        _ensure_orchestrator_skills_in_bundle(fresh_bundle, agent_spec=None)
        assert (fresh_bundle / "skills" / "build-omnigent").exists()

    missing_source_bundle = tmp_path / "missing-source"
    missing_source_bundle.mkdir()
    with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
        _ensure_orchestrator_skills_in_bundle(missing_source_bundle, agent_spec=None)


def test_ensure_orchestrator_skills_symlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle_dir = tmp_path / "symlink-bundle"
    bundle_dir.mkdir()
    source = (
        Path(__file__).resolve().parents[2]
        / "omnigent"
        / "onboarding"
        / "agent"
        / "skills"
        / "build-omnigent"
    )
    if not source.is_dir():
        pytest.skip("build-omnigent skill source not present in checkout")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("symlink denied")

    monkeypatch.setattr(Path, "symlink_to", _boom, raising=False)
    with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
        _ensure_orchestrator_skills_in_bundle(bundle_dir, agent_spec=None)
    assert "Could not link build-omnigent skill" in caplog.text