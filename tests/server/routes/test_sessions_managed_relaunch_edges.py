"""Edge tests for managed-sandbox relaunch helpers in ``sessions.py``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.managed_hosts import (
    MANAGED_REPO_LABEL_KEY,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
)
from omnigent.server.routes.sessions import (
    _kick_managed_relaunch,
    _managed_launch_tasks,
    _maybe_relaunch_managed_sandbox,
)
from omnigent.stores.host_store import Host
from tests.server.helpers import FakeSandboxLauncher

pytestmark = pytest.mark.asyncio


def _sandbox_config() -> ManagedSandboxConfig:
    return ManagedSandboxConfig(
        server_url="https://srv.example.com",
        launcher_factory=lambda: FakeSandboxLauncher(),
        token_ttl_s=3600,
    )


def _managed_conv(conv_id: str = "conv_relaunch") -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=conv_id,
        host_id="host_managed",
        workspace="/root/workspace",
        labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo.git"},
    )


def _managed_host(*, online: bool = False) -> Host:
    return Host(
        host_id="host_managed",
        name="sandbox",
        owner="alice@example.com",
        status="online" if online else "offline",
        created_at=0,
        updated_at=0,
        sandbox_provider="modal",
        sandbox_id="sb-1",
    )


def _app_state(
    *,
    host_store: object | None = MagicMock(),
    sandbox_config: object | None = None,
    tracker: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        host_store=host_store,
        sandbox_config=sandbox_config or _sandbox_config(),
        managed_launches=tracker or ManagedLaunchTracker(),
        host_registry=MagicMock(),
        tunnel_registry=MagicMock(),
    )


async def test_maybe_relaunch_returns_false_when_managed_hosts_unconfigured() -> None:
    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_x",
        conv=_managed_conv(),
        app_state=SimpleNamespace(),
        conversation_store=MagicMock(),
    )
    assert result is False


async def test_maybe_relaunch_returns_false_when_session_has_no_host() -> None:
    conv = Conversation(
        id="conv_no_host",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_no_host",
        host_id=None,
    )
    host_store = MagicMock()
    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_no_host",
        conv=conv,
        app_state=_app_state(host_store=host_store),
        conversation_store=MagicMock(),
    )
    assert result is False
    host_store.get_host.assert_not_called()


async def test_maybe_relaunch_returns_false_for_non_managed_host() -> None:
    host_store = MagicMock()
    host_store.get_host = MagicMock(
        return_value=Host(
            host_id="host_laptop",
            name="laptop",
            owner="alice@example.com",
            status="offline",
            created_at=0,
            updated_at=0,
            sandbox_provider=None,
            sandbox_id=None,
        )
    )
    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_laptop",
        conv=_managed_conv("conv_laptop"),
        app_state=_app_state(host_store=host_store),
        conversation_store=MagicMock(),
    )
    assert result is False


async def test_maybe_relaunch_returns_false_when_host_still_online() -> None:
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=_managed_host(online=True))
    host_store.is_online = MagicMock(return_value=True)

    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_online",
        conv=_managed_conv("conv_online"),
        app_state=_app_state(host_store=host_store),
        conversation_store=MagicMock(),
    )
    assert result is False


async def test_maybe_relaunch_kicks_background_task_for_dead_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=_managed_host(online=False))
    host_store.is_online = MagicMock(return_value=False)
    tracker = ManagedLaunchTracker()
    awaited: list[ManagedLaunch] = []

    async def _fake_await(launch: ManagedLaunch) -> None:
        awaited.append(launch)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._await_settled_managed_launch",
        _fake_await,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._run_managed_launch",
        AsyncMock(),
    )

    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_dead",
        conv=_managed_conv("conv_dead"),
        app_state=_app_state(host_store=host_store, tracker=tracker),
        conversation_store=MagicMock(),
    )

    assert result is True
    assert tracker.get("conv_dead") is not None
    assert awaited


async def test_kick_managed_relaunch_registers_task_and_parses_repo_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ManagedLaunchTracker()
    stages: list[str] = []
    run_calls: list[dict[str, object]] = []

    async def _fake_run_managed_launch(**kwargs: object) -> None:
        run_calls.append(kwargs)
        tracker.finish(str(kwargs["session_id"]))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._run_managed_launch",
        _fake_run_managed_launch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda _sid, stage, error=None: stages.append(stage),
    )

    _managed_launch_tasks.clear()
    _kick_managed_relaunch(
        session_id="conv_kick",
        conv=_managed_conv("conv_kick"),
        host=_managed_host(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=MagicMock(),
        host_store=MagicMock(),
        app_state=_app_state(tracker=tracker),
    )

    await asyncio.sleep(0.05)
    assert stages == ["provisioning"]
    assert run_calls
    assert run_calls[0]["relaunch_host"] is not None
    assert run_calls[0]["repo"] is not None


async def test_kick_managed_relaunch_ignores_unparseable_repo_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ManagedLaunchTracker()
    run_calls: list[dict[str, object]] = []

    async def _fake_run_managed_launch(**kwargs: object) -> None:
        run_calls.append(kwargs)
        tracker.finish(str(kwargs["session_id"]))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._run_managed_launch",
        _fake_run_managed_launch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda *_a, **_k: None,
    )

    conv = _managed_conv("conv_bad_repo")
    conv.labels[MANAGED_REPO_LABEL_KEY] = "not-a-valid-repo-url"

    _managed_launch_tasks.clear()
    _kick_managed_relaunch(
        session_id="conv_bad_repo",
        conv=conv,
        host=_managed_host(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=MagicMock(),
        host_store=MagicMock(),
        app_state=_app_state(tracker=tracker),
    )

    await asyncio.sleep(0.05)
    assert run_calls
    assert run_calls[0]["repo"] is None


async def test_maybe_relaunch_awaits_existing_inflight_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=_managed_host(online=False))
    host_store.is_online = MagicMock(return_value=False)
    tracker = ManagedLaunchTracker()
    tracker.begin("conv_inflight")
    kick_called = False

    def _should_not_kick(**_kwargs: object) -> None:
        nonlocal kick_called
        kick_called = True

    async def _fake_await(inflight: ManagedLaunch) -> None:
        inflight.settled.set()

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._kick_managed_relaunch",
        _should_not_kick,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._await_settled_managed_launch",
        _fake_await,
    )

    result = await _maybe_relaunch_managed_sandbox(
        session_id="conv_inflight",
        conv=_managed_conv("conv_inflight"),
        app_state=_app_state(host_store=host_store, tracker=tracker),
        conversation_store=MagicMock(),
    )

    assert result is True
    assert kick_called is False


async def test_maybe_relaunch_surfaces_settled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=_managed_host(online=False))
    host_store.is_online = MagicMock(return_value=False)
    tracker = ManagedLaunchTracker()
    tracker.begin("conv_failed")
    tracker.fail("conv_failed", "provider quota")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._kick_managed_relaunch",
        lambda **_kwargs: None,
    )

    with pytest.raises(OmnigentError) as exc:
        await _maybe_relaunch_managed_sandbox(
            session_id="conv_failed",
            conv=_managed_conv("conv_failed"),
            app_state=_app_state(host_store=host_store, tracker=tracker),
            conversation_store=MagicMock(),
        )

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "provider quota" in str(exc.value)
