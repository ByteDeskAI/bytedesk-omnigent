from __future__ import annotations

from dataclasses import dataclass

import pytest

from omnigent.fabric.runner_fabric import (
    FabricHostLaunchResult,
    HostRunnerAcquisition,
    HostWorkerRunnerFabric,
)


@dataclass
class _FakeStore:
    mode: str | None = None
    session_id: str | None = None
    runner_id: str | None = None

    def set_runner_id(self, session_id: str, runner_id: str) -> bool:
        self.mode = "set"
        self.session_id = session_id
        self.runner_id = runner_id
        return True

    def replace_runner_id(self, session_id: str, runner_id: str) -> None:
        self.mode = "replace"
        self.session_id = session_id
        self.runner_id = runner_id


class _FakeRegistry:
    def __init__(self) -> None:
        self.recorded: tuple[str, str, str] | None = None
        self.events: list[str] = []

    def record_launch_owner(self, runner_id: str, owner: str, *, token: str) -> None:
        self.events.append("record_owner")
        self.recorded = (runner_id, owner, token)


class _FakeCredentialStore:
    def __init__(self) -> None:
        self.recorded: tuple[str, str, str] | None = None

    async def record_launch_token(self, runner_id: str, owner: str, token: str) -> None:
        self.recorded = (runner_id, owner, token)


class _FakeHostWorker:
    def __init__(self, *, acked: bool = True) -> None:
        self.acked = acked
        self.calls: list[dict] = []

    async def launch_runner(self, **kwargs) -> FabricHostLaunchResult:
        self.calls.append(kwargs)
        return FabricHostLaunchResult(
            result={"status": "ok", "runner_id": None, "error": None, "error_code": None},
            acked=self.acked,
        )


@pytest.mark.asyncio
async def test_runner_fabric_atomically_binds_and_launches_host_runner() -> None:
    store = _FakeStore()
    owner_registry = _FakeRegistry()
    credential_store = _FakeCredentialStore()
    host_worker = _FakeHostWorker()
    fabric = HostWorkerRunnerFabric(host_worker=host_worker)

    host_registry = object()

    result = await fabric.ensure_runner(
        HostRunnerAcquisition(
            session_id="conv_1",
            host_id="host_1",
            workspace="/work/repo",
            harness="claude-sdk",
            conversation_store=store,
            host_registry=host_registry,
            owner="alice@example.com",
            runner_control_registry=owner_registry,
            runner_credential_store=credential_store,
            bind_mode="set",
            timeout_s=15.0,
        )
    )

    assert result.runner_id.startswith("runner_")
    assert result.acked is True
    assert store.mode == "set"
    assert store.session_id == "conv_1"
    assert store.runner_id == result.runner_id
    assert owner_registry.recorded is not None
    recorded_runner, recorded_owner, recorded_token = owner_registry.recorded
    assert recorded_runner == result.runner_id
    assert recorded_owner == "alice@example.com"
    assert recorded_token
    assert credential_store.recorded == (
        result.runner_id,
        "alice@example.com",
        recorded_token,
    )
    assert len(host_worker.calls) == 1
    call = host_worker.calls[0]
    assert call["host_registry"] is host_registry
    assert call["host_id"] == "host_1"
    assert call["binding_token"] == recorded_token
    assert call["workspace"] == "/work/repo"
    assert call["harness"] == "claude-sdk"
    assert call["timeout_s"] == 15.0
    assert call["host_connection"] is None


@pytest.mark.asyncio
async def test_runner_fabric_replaces_runner_binding_for_relaunch() -> None:
    store = _FakeStore()
    host_worker = _FakeHostWorker(acked=False)
    fabric = HostWorkerRunnerFabric(host_worker=host_worker)

    result = await fabric.ensure_runner(
        HostRunnerAcquisition(
            session_id="conv_2",
            host_id="host_2",
            workspace="/work/repo",
            harness=None,
            conversation_store=store,
            host_registry=object(),
            bind_mode="replace",
            timeout_s=5.0,
        )
    )

    assert store.mode == "replace"
    assert store.runner_id == result.runner_id
    assert result.acked is False


@pytest.mark.asyncio
async def test_runner_fabric_runs_pre_launch_hook_between_bind_and_launch() -> None:
    store = _FakeStore()
    owner_registry = _FakeRegistry()
    host_worker = _FakeHostWorker()
    fabric = HostWorkerRunnerFabric(host_worker=host_worker)
    events: list[str] = []

    async def before_launch(runner_id: str) -> None:
        events.append(f"before:{runner_id}")

    result = await fabric.ensure_runner(
        HostRunnerAcquisition(
            session_id="conv_3",
            host_id="host_3",
            workspace="/work/repo",
            harness=None,
            conversation_store=store,
            host_registry=object(),
            owner="alice@example.com",
            runner_control_registry=owner_registry,
            bind_mode="set",
            before_launch=before_launch,
        )
    )

    assert store.runner_id == result.runner_id
    assert events == [f"before:{result.runner_id}"]
    assert owner_registry.events == ["record_owner"]
    assert len(host_worker.calls) == 1
