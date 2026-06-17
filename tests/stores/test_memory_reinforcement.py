"""Tests for FU1 out-of-band reinforcement (BDP-2147 T8, ADR-0132)."""

from __future__ import annotations

import json
import time

import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory
from omnigent.stores.memory_store import ReinforcementBuffer, SqlAlchemyMemoryStore

_DAY = 86_400


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")


def _engine(tmp_path):
    return sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")


def test_reinforce_resets_clock_increments_count_not_weight(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    a = store.append(scope="agent", owner="m", name="n", content="alpha", weight=1.0, now=base)
    b = store.append(scope="agent", owner="m", name="n", content="beta", weight=1.0, now=base)
    c = store.append(scope="agent", owner="m", name="n", content="gamma", weight=1.0, now=base)

    assert store.reinforce([a, b], now=base + 500) == 2

    with Session(_engine(tmp_path)) as s:
        ra = s.get(SqlMemory, a)
        rc = s.get(SqlMemory, c)
        assert ra.last_accessed_at == base + 500
        assert ra.access_count == 1
        assert ra.weight == 1.0, "reinforcement must not inflate base weight"
        assert rc.last_accessed_at == base and rc.access_count == 0, "untouched memory unchanged"


def test_reinforce_skips_archived(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    a = store.append(
        scope="topic", owner="shared", name="t", content="x", half_life_seconds=100, now=base
    )
    assert store.sweep(now=base + 40 * _DAY) == 1  # archives a
    assert store.reinforce([a], now=base + 40 * _DAY) == 0


def test_buffer_dedupes_rate_limits_and_flushes(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    a = store.append(scope="agent", owner="m", name="n", content="alpha", now=base)

    buf = ReinforcementBuffer(min_interval_seconds=60)
    buf.record([a, a, a], now=base)  # dedupe
    assert buf.pending_count() == 1

    assert buf.flush(store, now=base + 10) == 1
    assert buf.pending_count() == 0
    with Session(_engine(tmp_path)) as s:
        assert s.get(SqlMemory, a).last_accessed_at == base + 10

    # Within the min-interval window of the last flush -> rate-limited (dropped).
    buf.record([a], now=base + 20)
    assert buf.pending_count() == 0
    # Past the window -> accepted again.
    buf.record([a], now=base + 10 + 61)
    assert buf.pending_count() == 1


def test_query_builtin_reinforces_out_of_band(tmp_path, monkeypatch) -> None:
    """memory_query records recalled ids to the buffer but does NOT write the
    DB inline — the recall stays a pure read; the flush does the write."""
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.memory import MemoryAppendTool, MemoryQueryTool

    store = _store(tmp_path)
    buf = ReinforcementBuffer(min_interval_seconds=60)
    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "omnigent.stores.memory_store.get_reinforcement_buffer", lambda: buf
    )
    ctx = ToolContext(task_id="t", agent_id="ag_m", conversation_id="c")

    MemoryAppendTool().invoke(json.dumps({"content": "alpha fact"}), ctx)
    with Session(_engine(tmp_path)) as s:
        before = sorted(r.last_accessed_at for r in s.execute(sa.select(SqlMemory)).scalars())

    res = json.loads(MemoryQueryTool().invoke(json.dumps({"query": "alpha"}), ctx))
    assert len(res["results"]) == 1
    assert buf.pending_count() == 1, "recall should buffer the hit id"
    with Session(_engine(tmp_path)) as s:
        after = sorted(r.last_accessed_at for r in s.execute(sa.select(SqlMemory)).scalars())
    assert before == after, "recall must not write last_accessed_at inline (pure read)"

    assert buf.flush(store) >= 1, "flush reinforces the buffered ids"
