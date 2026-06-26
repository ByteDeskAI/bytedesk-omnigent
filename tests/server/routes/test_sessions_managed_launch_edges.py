"""Edge tests for managed-sandbox launch helpers in ``sessions.py``."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HARNESS_NOT_CONFIGURED_ERROR_CODE
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
)
from omnigent.server.routes.sessions import (
    _await_settled_managed_launch,
    _bind_and_launch_managed_runner,
    _HostLaunchAttempt,
    _provision_managed_sandbox,
    _run_managed_launch,
)
from omnigent.stores.conversation_store import ConversationNotFoundError
from omnigent.stores.host_store import Host
from tests.server.helpers import FakeSandboxLauncher

pytestmark = pytest.mark.asyncio


def _sandbox_config() -> ManagedSandboxConfig:
    return ManagedSandboxConfig(
        server_url="https://srv.example.com",
        launcher_factory=lambda: FakeSandboxLauncher(),
        token_ttl_s=3600,
    )


def _managed_launch(host_id: str = "host_managed") -> ManagedHostLaunch:
    return ManagedHostLaunch(host_id=host_id, workspace="/root/workspace")


def _host_row(host_id: str = "host_managed") -> Host:
    return Host(
        host_id=host_id,
        name="sandbox",
        owner="alice@example.com",
        status="online",
        created_at=0,
        updated_at=0,
        sandbox_provider="modal",
        sandbox_id="sb-1",
    )


async def test_provision_managed_sandbox_launches_fresh_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages: list[str] = []
    launched = _managed_launch()

    async def _fake_launch(**kwargs: Any) -> ManagedHostLaunch:
        on_stage = kwargs.get("on_stage")
        if on_stage is not None:
            on_stage("provisioning")
        return launched

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.launch_managed_host",
        _fake_launch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda sid, stage, error=None: stages.append(stage),
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_prov")
    result = await _provision_managed_sandbox(
        session_id="conv_prov",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=tracker,
        host_store=MagicMock(),
        relaunch_host=None,
    )

    assert result is launched
    assert stages == ["provisioning"]


async def test_provision_managed_sandbox_relaunches_existing_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaunched = _managed_launch("host_relaunch")

    async def _fake_relaunch(**_kwargs: Any) -> ManagedHostLaunch:
        return relaunched

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.relaunch_managed_host",
        _fake_relaunch,
    )

    result = await _provision_managed_sandbox(
        session_id="conv_relaunch",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=ManagedLaunchTracker(),
        host_store=MagicMock(),
        relaunch_host=_host_row(),
    )

    assert result is relaunched


async def test_provision_managed_sandbox_fails_on_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses: list[tuple[str, str | None]] = []

    async def _boom(**_kwargs: Any) -> ManagedHostLaunch:
        raise HTTPException(status_code=503, detail="spend limit reached")

    monkeypatch.setattr("omnigent.server.managed_hosts.launch_managed_host", _boom)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda sid, stage, error=None: statuses.append((stage, error)),
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_http")
    result = await _provision_managed_sandbox(
        session_id="conv_http",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=tracker,
        host_store=MagicMock(),
        relaunch_host=None,
    )

    assert result is None
    entry = tracker.get("conv_http")
    assert entry is not None
    assert entry.error == "spend limit reached"
    assert statuses[-1] == ("failed", "spend limit reached")


async def test_provision_managed_sandbox_fails_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _crash(**_kwargs: Any) -> ManagedHostLaunch:
        raise RuntimeError("provider offline")

    monkeypatch.setattr("omnigent.server.managed_hosts.launch_managed_host", _crash)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda *_a, **_k: None,
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_crash")
    result = await _provision_managed_sandbox(
        session_id="conv_crash",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=tracker,
        host_store=MagicMock(),
        relaunch_host=None,
    )

    assert result is None
    entry = tracker.get("conv_crash")
    assert entry is not None
    assert entry.error == "internal error during managed sandbox launch"


async def test_bind_and_launch_managed_runner_tears_down_when_session_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []
    statuses: list[tuple[str, str | None]] = []

    store = MagicMock()
    store.set_host_id = MagicMock(
        side_effect=ConversationNotFoundError("conv_deleted"),
    )
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=_host_row())

    async def _fake_terminate(host: Host, hs: object, cfg: object) -> None:
        terminated.append(host.host_id)

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.terminate_managed_host",
        _fake_terminate,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda sid, stage, error=None: statuses.append((stage, error)),
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_deleted")
    await _bind_and_launch_managed_runner(
        session_id="conv_deleted",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=host_store,
        host_registry=None,
        runner_control_registry=None,
    )

    assert terminated == ["host_managed"]
    entry = tracker.get("conv_deleted")
    assert entry is not None
    assert "deleted" in (entry.error or "")
    assert statuses[-1][0] == "failed"


async def test_bind_and_launch_managed_runner_fails_on_harness_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_harness",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_harness",
        host_id="host_managed",
        workspace="/root/workspace",
    )
    store = MagicMock()
    store.set_host_id = MagicMock(return_value=conv)

    registry = MagicMock()
    registry.get = MagicMock(return_value=MagicMock())

    async def _fake_launch(*_args: Any, **_kwargs: Any) -> _HostLaunchAttempt:
        return _HostLaunchAttempt(
            runner_id="runner_x",
            error_code=HARNESS_NOT_CONFIGURED_ERROR_CODE,
            error="harness missing",
        )

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._launch_runner_on_host",
        _fake_launch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda *_a, **_k: None,
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_harness")
    await _bind_and_launch_managed_runner(
        session_id="conv_harness",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=MagicMock(),
        host_registry=registry,
        runner_control_registry=MagicMock(),
    )

    entry = tracker.get("conv_harness")
    assert entry is not None
    assert entry.error == "harness missing"


async def test_bind_and_launch_managed_runner_finishes_when_runner_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_ready",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_ready",
        host_id="host_managed",
        workspace="/root/workspace",
    )
    store = MagicMock()
    store.set_host_id = MagicMock(return_value=conv)

    registry = MagicMock()
    registry.get = MagicMock(return_value=MagicMock())
    tunnel = MagicMock()
    runner_router = MagicMock()
    waited: dict[str, Any] = {}
    client = httpx.AsyncClient()

    async def _fake_launch(*_args: Any, **_kwargs: Any) -> _HostLaunchAttempt:
        return _HostLaunchAttempt(runner_id="runner_ok")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._launch_runner_on_host",
        _fake_launch,
    )

    async def _fake_wait_for_runner_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        waited["args"] = args
        waited["kwargs"] = kwargs
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._wait_for_runner_client",
        _fake_wait_for_runner_client,
    )
    stages: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda sid, stage, error=None: stages.append(stage),
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_ready")
    await _bind_and_launch_managed_runner(
        session_id="conv_ready",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=MagicMock(),
        host_registry=registry,
        runner_control_registry=tunnel,
        runner_router=runner_router,
    )

    assert tracker.get("conv_ready") is None
    assert waited["args"][:3] == ("conv_ready", runner_router, tunnel)
    assert waited["kwargs"]["runner_id"] == "runner_ok"
    assert waited["kwargs"]["timeout_s"] == 30.0
    assert "connecting" in stages
    assert stages[-1] == "ready"
    await client.aclose()


async def test_run_managed_launch_returns_when_provision_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind_called = False

    async def _fake_provision(**_kwargs: Any) -> None:
        return None

    async def _fake_bind(**_kwargs: Any) -> None:
        nonlocal bind_called
        bind_called = True

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._provision_managed_sandbox",
        _fake_provision,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._bind_and_launch_managed_runner",
        _fake_bind,
    )

    await _run_managed_launch(
        session_id="conv_skip",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=ManagedLaunchTracker(),
        conversation_store=MagicMock(),
        host_store=MagicMock(),
        host_registry=None,
        runner_control_registry=None,
    )

    assert bind_called is False


async def test_run_managed_launch_calls_bind_after_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind_called = False
    managed = _managed_launch()

    async def _fake_provision(**_kwargs: Any) -> ManagedHostLaunch:
        return managed

    async def _fake_bind(**kwargs: Any) -> None:
        nonlocal bind_called
        bind_called = True
        assert kwargs["managed"] is managed

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._provision_managed_sandbox",
        _fake_provision,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._bind_and_launch_managed_runner",
        _fake_bind,
    )

    await _run_managed_launch(
        session_id="conv_pipe",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=ManagedLaunchTracker(),
        conversation_store=MagicMock(),
        host_store=MagicMock(),
        host_registry=None,
        runner_control_registry=None,
    )

    assert bind_called is True


async def test_await_settled_managed_launch_raises_on_recorded_error() -> None:
    launch = ManagedLaunch(settled=asyncio.Event(), error="sandbox died")
    launch.settled.set()

    with pytest.raises(OmnigentError) as exc:
        await _await_settled_managed_launch(launch)

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "sandbox died" in str(exc.value)


async def test_await_settled_managed_launch_times_out_when_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.managed_hosts.MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S",
        0.01,
    )
    launch = ManagedLaunch(settled=asyncio.Event())

    with pytest.raises(OmnigentError) as exc:
        await _await_settled_managed_launch(launch)

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "still provisioning" in str(exc.value)


async def test_await_settled_managed_launch_returns_when_settled_successfully() -> None:
    launch = ManagedLaunch(settled=asyncio.Event())
    launch.settled.set()
    await _await_settled_managed_launch(launch)


async def test_bind_and_launch_managed_runner_finishes_without_host_registry() -> None:
    conv = Conversation(
        id="conv_no_registry",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_no_registry",
        host_id="host_managed",
        workspace="/root/workspace",
    )
    store = MagicMock()
    store.set_host_id = MagicMock(return_value=conv)

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_no_registry")
    await _bind_and_launch_managed_runner(
        session_id="conv_no_registry",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=MagicMock(),
        host_registry=None,
        runner_control_registry=None,
    )

    assert tracker.get("conv_no_registry") is None


async def test_bind_and_launch_managed_runner_skips_launch_when_host_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_no_conn",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_no_conn",
        host_id="host_managed",
        workspace="/root/workspace",
    )
    store = MagicMock()
    store.set_host_id = MagicMock(return_value=conv)
    registry = MagicMock()
    registry.get = MagicMock(return_value=None)
    launch_called = False

    async def _should_not_launch(*_args: Any, **_kwargs: Any) -> _HostLaunchAttempt:
        nonlocal launch_called
        launch_called = True
        return _HostLaunchAttempt(runner_id="runner_x")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._launch_runner_on_host",
        _should_not_launch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda *_a, **_k: None,
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_no_conn")
    await _bind_and_launch_managed_runner(
        session_id="conv_no_conn",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=MagicMock(),
        host_registry=registry,
        runner_control_registry=MagicMock(),
    )

    assert launch_called is False
    assert tracker.get("conv_no_conn") is None


async def test_bind_and_launch_managed_runner_delete_skips_terminate_when_host_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated = False
    store = MagicMock()
    store.set_host_id = MagicMock(side_effect=ConversationNotFoundError("conv_gone"))
    host_store = MagicMock()
    host_store.get_host = MagicMock(return_value=None)

    async def _fake_terminate(*_args: Any, **_kwargs: Any) -> None:
        nonlocal terminated
        terminated = True

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.terminate_managed_host",
        _fake_terminate,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_sandbox_status",
        lambda *_a, **_k: None,
    )

    tracker = ManagedLaunchTracker()
    tracker.begin("conv_gone")
    await _bind_and_launch_managed_runner(
        session_id="conv_gone",
        owner="alice@example.com",
        managed=_managed_launch(),
        sandbox_config=_sandbox_config(),
        tracker=tracker,
        conversation_store=store,
        host_store=host_store,
        host_registry=None,
        runner_control_registry=None,
    )

    assert terminated is False
    entry = tracker.get("conv_gone")
    assert entry is not None
    assert "deleted" in (entry.error or "")


async def test_run_managed_launch_passes_relaunch_host_to_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _fake_provision(**kwargs: Any) -> ManagedHostLaunch:
        seen.update(kwargs)
        return _managed_launch()

    async def _fake_bind(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._provision_managed_sandbox",
        _fake_provision,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._bind_and_launch_managed_runner",
        _fake_bind,
    )

    relaunch = _host_row()
    await _run_managed_launch(
        session_id="conv_relaunch_pipe",
        owner="alice@example.com",
        sandbox_config=_sandbox_config(),
        repo=None,
        tracker=ManagedLaunchTracker(),
        conversation_store=MagicMock(),
        host_store=MagicMock(),
        host_registry=None,
        runner_control_registry=None,
        relaunch_host=relaunch,
    )

    assert seen["relaunch_host"] is relaunch
