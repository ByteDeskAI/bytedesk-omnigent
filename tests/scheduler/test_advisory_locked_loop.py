"""Contract test for the shared advisory-locked maintenance-loop factory (BDP-2355).

Proves the Template-Method scaffold: a tick acquires the lock and runs the work;
a tick that can't acquire the lock skips the work; a failed tick is swallowed and
the loop continues; cancellation propagates. The three real loops (signal-bus
reaper, cron scheduler, accountability) keep their own behavior tests — this pins
the scaffold they now share.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

import pytest

import bytedesk_omnigent.maintenance as maintenance
from bytedesk_omnigent.maintenance import advisory_locked_loop

pytestmark = pytest.mark.asyncio


@contextmanager
def _fake_lock(acquired: bool):
    yield acquired


async def _run_one_tick(monkeypatch, *, acquired: bool, work, on_sleep=None):
    """Drive exactly one tick: the first ``asyncio.sleep`` returns, the second
    raises ``CancelledError`` to end the loop after one iteration."""
    calls = {"n": 0}

    async def _sleep(_seconds):
        calls["n"] += 1
        if on_sleep is not None:
            on_sleep(calls["n"])
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(maintenance.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        maintenance, "advisory_lock", lambda engine, key: _fake_lock(acquired)
    )

    def _prepare():
        return object(), work

    with pytest.raises(asyncio.CancelledError):
        await advisory_locked_loop(
            interval_seconds=0,
            lock_key=1,
            prepare=_prepare,
            logger=logging.getLogger("test"),
            name="test loop",
        )


async def test_runs_work_when_lock_acquired(monkeypatch) -> None:
    ran = {"n": 0}

    async def _work() -> None:
        ran["n"] += 1

    await _run_one_tick(monkeypatch, acquired=True, work=_work)
    assert ran["n"] == 1  # work ran exactly once under the acquired lock


async def test_skips_work_when_lock_not_acquired(monkeypatch) -> None:
    ran = {"n": 0}

    async def _work() -> None:
        ran["n"] += 1

    await _run_one_tick(monkeypatch, acquired=False, work=_work)
    assert ran["n"] == 0  # another instance holds the lock → tick skipped


async def test_failed_tick_is_swallowed_and_loop_continues(monkeypatch, caplog) -> None:
    """A worker that raises is logged (named) and the loop keeps going to the
    next sleep — it does not crash the background task."""

    async def _work() -> None:
        raise RuntimeError("boom")

    sleeps_seen: list[int] = []

    with caplog.at_level(logging.WARNING):
        await _run_one_tick(
            monkeypatch, acquired=True, work=_work, on_sleep=sleeps_seen.append
        )

    # Reached the SECOND sleep → the loop survived the failed first tick.
    assert sleeps_seen == [1, 2]
    assert any("test loop tick failed" in r.message for r in caplog.records)


async def test_cancellation_propagates(monkeypatch) -> None:
    """CancelledError raised inside the worker is re-raised (clean shutdown), not
    swallowed by the resilient-except."""

    async def _work() -> None:
        raise asyncio.CancelledError

    # First sleep returns so the single tick runs; the worker raises Cancelled.
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        maintenance, "advisory_lock", lambda engine, key: _fake_lock(True)
    )

    def _prepare():
        return object(), _work

    with pytest.raises(asyncio.CancelledError):
        await advisory_locked_loop(
            interval_seconds=0,
            lock_key=1,
            prepare=_prepare,
            logger=logging.getLogger("test"),
            name="test loop",
        )
