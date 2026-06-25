"""Tests for the signal-bus reaper: expire stale waits + advisory-lock no-op
(BDP-2248, ADR-0142)."""

from __future__ import annotations

import time

import pytest

from bytedesk_omnigent.bus import SqlAlchemySignalBus
from bytedesk_omnigent.bus.reaper import (
    _SIGNAL_BUS_LOCK_KEY,
    run_signal_bus_sweep_tick,
    signal_bus_reaper_loop,
)
from omnigent.runtime.memory_maintenance import advisory_lock

pytestmark = pytest.mark.asyncio


def _bus(tmp_path) -> SqlAlchemySignalBus:
    return SqlAlchemySignalBus(f"sqlite:///{tmp_path / 'bus.db'}")


def test_sweep_expired_marks_pending_past_expiry(tmp_path) -> None:
    bus = _bus(tmp_path)
    now = int(time.time())
    # One wait already past its expiry, one still live.
    bus.register_wait(
        signal_id="a:1",
        session_id="s",
        key="k",
        kind="subscribe",
        target="t",
        expires_at=now - 1,
        now=now - 10,
    )
    bus.register_wait(
        signal_id="b:1",
        session_id="s",
        key="k",
        kind="subscribe",
        target="t",
        expires_at=now + 1000,
        now=now,
    )

    swept = run_signal_bus_sweep_tick(bus, now=now)
    assert swept == 1
    # Only the live wait remains pending; the expired one is gone from the registry.
    assert [w.signal_id for w in bus.list_pending(target="t")] == ["b:1"]


def test_advisory_lock_noop_on_sqlite(tmp_path) -> None:
    bus = _bus(tmp_path)
    with advisory_lock(bus.engine, _SIGNAL_BUS_LOCK_KEY) as acquired:
        assert acquired is True


async def test_signal_bus_reaper_loop_sweeps_expired_waits(monkeypatch, tmp_path) -> None:
    bus = _bus(tmp_path)
    now = int(time.time())
    bus.register_wait(
        signal_id="expired:1",
        session_id="s",
        key="k",
        kind="subscribe",
        target="t",
        expires_at=now - 1,
        now=now - 10,
    )
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: bus)

    async def _fake_locked_loop(*, interval_seconds, lock_key, prepare, logger, name):
        engine, work = prepare()
        assert engine is bus.engine
        await work()

    monkeypatch.setattr(
        "bytedesk_omnigent.bus.reaper.advisory_locked_loop",
        _fake_locked_loop,
    )

    await signal_bus_reaper_loop(interval_seconds=0)
    assert [w.signal_id for w in bus.list_pending(target="t")] == []
