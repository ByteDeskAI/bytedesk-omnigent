"""Tests for the cron scheduler background loop (BDP-2250)."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager

import pytest

import bytedesk_omnigent.maintenance as maintenance
from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler
from bytedesk_omnigent.scheduler.loop import _log_only_dispatch, cron_scheduler_loop
from bytedesk_omnigent.scheduler.scheduler import CronTrigger

pytestmark = pytest.mark.asyncio


@contextmanager
def _fake_lock(acquired: bool):
    yield acquired


async def _run_loop_one_tick(monkeypatch, *, work_prepare, caplog=None):
    calls = {"n": 0}

    async def _sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(maintenance.asyncio, "sleep", _sleep)
    monkeypatch.setattr(maintenance, "advisory_lock", lambda engine, key: _fake_lock(True))
    monkeypatch.setattr(
        "bytedesk_omnigent.scheduler.loop.advisory_locked_loop",
        work_prepare,
    )

    with pytest.raises(asyncio.CancelledError):
        await cron_scheduler_loop(interval_seconds=0)


def test_log_only_dispatch_logs_trigger(caplog) -> None:
    trigger = CronTrigger(
        id="t1",
        agent_id="maya",
        key="standup",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=0,
        enabled=True,
        payload={},
        version=1,
    )
    with caplog.at_level(logging.INFO):
        _log_only_dispatch(trigger)
    assert "cron fire (no dispatch wired yet)" in caplog.text


async def test_cron_scheduler_loop_fires_due_triggers(monkeypatch, tmp_path) -> None:
    sched = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    now = int(time.time())
    sched.register_trigger(
        agent_id="maya",
        key="standup",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=now,
        now=now,
    )
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_cron_scheduler", lambda: sched)

    captured: dict[str, object] = {}

    async def _fake_locked_loop(*, interval_seconds, lock_key, prepare, logger, name):
        engine, work = prepare()
        captured["engine"] = engine
        await work()

    monkeypatch.setattr(
        "bytedesk_omnigent.scheduler.loop.advisory_locked_loop",
        _fake_locked_loop,
    )

    await cron_scheduler_loop(interval_seconds=0)
    assert captured["engine"] is sched.engine
    assert sched.due_triggers(now=now) == []


async def test_cron_scheduler_loop_uses_custom_dispatch(monkeypatch, tmp_path) -> None:
    sched = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    now = int(time.time())
    sched.register_trigger(
        agent_id="caleb",
        key="review",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=now,
        now=now,
    )
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_cron_scheduler", lambda: sched)

    fired: list[str] = []

    async def _fake_locked_loop(*, interval_seconds, lock_key, prepare, logger, name):
        _, work = prepare()
        await work()

    monkeypatch.setattr(
        "bytedesk_omnigent.scheduler.loop.advisory_locked_loop",
        _fake_locked_loop,
    )

    await cron_scheduler_loop(
        interval_seconds=0,
        dispatch=lambda trigger: fired.append(trigger.agent_id),
    )
    assert fired == ["caleb"]
