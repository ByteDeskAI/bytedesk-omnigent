"""Tests for FU1 memory maintenance: flush-before-sweep + advisory lock
(BDP-2147 T9, ADR-0132)."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory
from omnigent.runtime.memory_maintenance import (
    advisory_lock,
    memory_maintenance_loop,
    run_memory_maintenance_tick,
)
from omnigent.stores.memory_store import ReinforcementBuffer, SqlAlchemyMemoryStore

_DAY = 86_400


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")


def test_tick_flushes_before_sweep_protecting_hot_rows(tmp_path) -> None:
    """A memory recalled just before the sweep (its id still buffered) has its
    clock reset by the flush and survives; an untouched decayed peer is evicted.
    This is the flush-before-sweep ordering guarantee."""
    store = _store(tmp_path)
    base = int(time.time())
    hot = store.append(
        scope="topic", owner="shared", name="t", content="hot row", half_life_seconds=100, now=base
    )
    cold = store.append(
        scope="topic", owner="shared", name="t", content="cold row", half_life_seconds=100, now=base
    )
    buffer = ReinforcementBuffer(min_interval_seconds=60)
    buffer.record([hot], now=base)  # hot row was recently recalled

    later = base + 40 * _DAY
    reinforced, archived = run_memory_maintenance_tick(store, buffer, now=later)
    assert reinforced == 1  # hot flushed (clock reset to `later`)
    assert archived == 1  # cold evicted

    with Session(sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")) as s:
        assert s.get(SqlMemory, hot).archived is False, "flush-before-sweep must protect a hot row"
        assert s.get(SqlMemory, cold).archived is True


def test_advisory_lock_noop_on_sqlite(tmp_path) -> None:
    store = _store(tmp_path)
    with advisory_lock(store.engine, 123) as acquired:
        assert acquired is True


def test_advisory_lock_postgresql_acquires_and_releases() -> None:
    """PostgreSQL path tries the advisory lock and unlocks on exit."""
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    conn = MagicMock()
    engine.connect.return_value = conn
    conn.execute.return_value.scalar.side_effect = [True, None]

    with advisory_lock(engine, 99) as acquired:
        assert acquired is True

    unlock_calls = [
        call
        for call in conn.execute.call_args_list
        if "pg_advisory_unlock" in str(call.args[0])
    ]
    assert unlock_calls
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_advisory_lock_postgresql_skips_tick_when_not_acquired() -> None:
    """When the advisory lock is busy, the context yields ``False``."""
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    conn = MagicMock()
    engine.connect.return_value = conn
    conn.execute.return_value.scalar.return_value = False

    with advisory_lock(engine, 99) as acquired:
        assert acquired is False

    conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_memory_maintenance_loop_runs_tick_and_logs_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The background loop flushes reinforcement and sweeps under the lock."""
    store = _store(tmp_path)
    buffer = ReinforcementBuffer(min_interval_seconds=0)
    tick = MagicMock(return_value=(2, 1))

    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "omnigent.stores.memory_store.get_reinforcement_buffer",
        lambda: buffer,
    )
    monkeypatch.setattr(
        "omnigent.runtime.memory_maintenance.run_memory_maintenance_tick",
        tick,
    )

    sleep_calls = 0

    async def stop_after_first_tick(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr("omnigent.runtime.memory_maintenance.asyncio.sleep", stop_after_first_tick)

    with pytest.raises(asyncio.CancelledError):
        await memory_maintenance_loop(interval_seconds=0)

    tick.assert_called_once_with(store, buffer)


@pytest.mark.asyncio
async def test_memory_maintenance_loop_skips_tick_when_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """When the advisory lock is not acquired, the loop skips the tick."""
    store = _store(tmp_path)
    tick = MagicMock()

    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "omnigent.stores.memory_store.get_reinforcement_buffer",
        lambda: ReinforcementBuffer(min_interval_seconds=0),
    )
    monkeypatch.setattr(
        "omnigent.runtime.memory_maintenance.run_memory_maintenance_tick",
        tick,
    )
    monkeypatch.setattr(
        "omnigent.runtime.memory_maintenance.advisory_lock",
        lambda *_a, **_k: _busy_lock(),
    )

    sleep_calls = 0

    async def stop_after_first_sleep(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr("omnigent.runtime.memory_maintenance.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await memory_maintenance_loop(interval_seconds=0)

    tick.assert_not_called()


@pytest.mark.asyncio
async def test_memory_maintenance_loop_logs_and_continues_on_tick_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A failed tick is logged and the loop keeps running until cancelled."""
    store = _store(tmp_path)

    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "omnigent.stores.memory_store.get_reinforcement_buffer",
        lambda: ReinforcementBuffer(min_interval_seconds=0),
    )
    monkeypatch.setattr(
        "omnigent.runtime.memory_maintenance.run_memory_maintenance_tick",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    sleep_calls = 0

    async def stop_after_first_sleep(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr("omnigent.runtime.memory_maintenance.asyncio.sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        await memory_maintenance_loop(interval_seconds=0)


@contextmanager
def _busy_lock():
    """Yield a lock context that reports not acquired."""
    yield False


@pytest.mark.asyncio
async def test_memory_maintenance_loop_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Cancellation during a tick propagates for clean shutdown."""
    store = _store(tmp_path)

    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "omnigent.stores.memory_store.get_reinforcement_buffer",
        lambda: ReinforcementBuffer(min_interval_seconds=0),
    )

    async def cancel_in_thread(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise asyncio.CancelledError()

    real_sleep = asyncio.sleep

    async def noop_sleep(_interval: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("omnigent.runtime.memory_maintenance.asyncio.to_thread", cancel_in_thread)
    monkeypatch.setattr("omnigent.runtime.memory_maintenance.asyncio.sleep", noop_sleep)

    with pytest.raises(asyncio.CancelledError):
        await memory_maintenance_loop(interval_seconds=0)
