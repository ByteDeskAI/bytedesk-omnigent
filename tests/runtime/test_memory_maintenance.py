"""Tests for FU1 memory maintenance: flush-before-sweep + advisory lock
(BDP-2147 T9, ADR-0132)."""

from __future__ import annotations

import time

import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory
from omnigent.runtime.memory_maintenance import advisory_lock, run_memory_maintenance_tick
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
