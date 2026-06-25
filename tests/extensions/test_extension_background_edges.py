"""Edge coverage for BytedeskExtension background lifespan tasks."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from bytedesk_omnigent.extension import BytedeskExtension


@pytest.mark.asyncio
async def test_cron_scheduler_registers_initiator_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = object()
    set_calls: list[object] = []

    async def _noop_loop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("bytedesk_omnigent.sessions.get_session_initiator", lambda: None)
    monkeypatch.setattr(
        "bytedesk_omnigent.sessions.build_self_call_initiator_from_env",
        lambda: built,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.sessions.set_session_initiator",
        lambda initiator: set_calls.append(initiator),
    )
    monkeypatch.setattr("bytedesk_omnigent.scheduler.cron_scheduler_loop", _noop_loop)

    await BytedeskExtension()._cron_scheduler()

    assert set_calls == [built]


@pytest.mark.asyncio
async def test_seed_workflow_tasks_swallows_store_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> None:
        raise RuntimeError("task store unavailable")

    monkeypatch.setattr("bytedesk_omnigent.tasks.get_task_store", _boom)

    await BytedeskExtension()._seed_workflow_tasks()


@pytest.mark.asyncio
async def test_cron_scheduler_warns_when_initiator_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _noop_loop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("bytedesk_omnigent.sessions.get_session_initiator", lambda: None)
    monkeypatch.setattr(
        "bytedesk_omnigent.sessions.build_self_call_initiator_from_env",
        lambda: None,
    )
    monkeypatch.setattr("bytedesk_omnigent.scheduler.cron_scheduler_loop", _noop_loop)

    with caplog.at_level("WARNING", logger="bytedesk_omnigent.extension"):
        await BytedeskExtension()._cron_scheduler()

    assert any("no SessionInitiator configured" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_seed_workflow_tasks_logs_count_when_lock_acquired(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Store:
        engine = object()

    @contextmanager
    def _lock(*_args: object, **_kwargs: object):
        yield True

    monkeypatch.setattr("bytedesk_omnigent.tasks.get_task_store", lambda: _Store())
    monkeypatch.setattr("omnigent.runtime.memory_maintenance.advisory_lock", _lock)
    monkeypatch.setattr(
        "bytedesk_omnigent.tasks.seed.seed_workflow_tasks",
        lambda **_kwargs: 3,
    )

    with caplog.at_level("INFO", logger="bytedesk_omnigent.extension"):
        await BytedeskExtension()._seed_workflow_tasks()

    assert any("workflow-task seed: 3" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_realtime_bridge_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    monkeypatch.setattr(
        "bytedesk_omnigent.realtime.install_realtime_bridge",
        lambda: called.append(True),
    )

    await BytedeskExtension()._realtime_bridge()
    assert called == [True]


@pytest.mark.asyncio
async def test_signal_bus_reaper_delegates_to_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    async def _fake_loop() -> None:
        seen.append(True)

    monkeypatch.setattr(
        "bytedesk_omnigent.bus.reaper.signal_bus_reaper_loop",
        _fake_loop,
    )

    await BytedeskExtension()._signal_bus_reaper()
    assert seen == [True]


@pytest.mark.asyncio
async def test_accountability_delegates_to_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object | None] = {}

    async def _fake_loop(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("OMNIGENT_ACCOUNTABILITY_MANAGER", "ag_mgr")
    monkeypatch.setattr(
        "bytedesk_omnigent.accountability.accountability_loop",
        _fake_loop,
    )

    await BytedeskExtension()._accountability()
    assert captured["manager_agent_id"] == "ag_mgr"


def test_config_descriptors_returns_bytedesk_registry() -> None:
    descriptors = BytedeskExtension().config_descriptors()
    assert descriptors
    assert all(hasattr(d, "key") for d in descriptors)


@pytest.mark.asyncio
async def test_tool_step_resume_logs_reclaimed_and_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store:
        engine = object()

        def resume_stale(self) -> int:
            return 2

    @contextmanager
    def _lock(*_args: object, **_kwargs: object):
        yield True

    monkeypatch.setattr("bytedesk_omnigent.runtime.get_tool_step_store", lambda: _Store())
    monkeypatch.setattr("omnigent.runtime.memory_maintenance.advisory_lock", _lock)

    await BytedeskExtension()._tool_step_resume()

    def _boom() -> None:
        raise RuntimeError("tool step store unavailable")

    monkeypatch.setattr("bytedesk_omnigent.runtime.get_tool_step_store", _boom)

    await BytedeskExtension()._tool_step_resume()
