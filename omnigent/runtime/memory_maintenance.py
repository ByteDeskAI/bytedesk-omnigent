"""Periodic memory maintenance: reinforcement flush + decay sweep (FU1 T9, ADR-0132).

Wired into the omnigent server's FastAPI lifespan as a background task. Each
tick FLUSHES the reinforcement buffer BEFORE running the decay sweep, so a
memory actively being recalled has its clock reset before the sweep evaluates
it for eviction — no archiving a hot row whose reinforcement is still buffered.

The tick is guarded by a PostgreSQL advisory lock so it runs on at most one
server instance at a time. omnigent-server is single-replica today (in-memory
runner registry), but the lock keeps the sweep correct if a shared registry
ever permits >1 replica (ADR-0132 topology note). On SQLite (local/dev/tests)
the lock is a no-op (single process). There is no DBOS in omnigent, so this is
a plain lifespan asyncio task, not a transactional scheduled step.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

from sqlalchemy import text

_logger = logging.getLogger(__name__)

# Stable 64-bit advisory-lock key for the memory-maintenance sweep ("mem_main").
_MEMORY_MAINTENANCE_LOCK_KEY = 0x6D656D5F6D61696E

_DEFAULT_INTERVAL_SECONDS = 300


def run_memory_maintenance_tick(store, buffer, *, now: int | None = None) -> tuple[int, int]:
    """Flush reinforcement, THEN sweep — ordering matters (see module docstring).

    :param store: The :class:`~omnigent.stores.memory_store.SqlAlchemyMemoryStore`.
    :param buffer: The :class:`~omnigent.stores.memory_store.ReinforcementBuffer`.
    :param now: Current epoch seconds; defaults inside each call.
    :returns: ``(reinforced_count, archived_count)``.
    """
    reinforced = buffer.flush(store, now=now)
    archived = store.sweep(now=now)
    return reinforced, archived


@contextmanager
def advisory_lock(engine, lock_key: int):
    """Best-effort cross-instance lock.

    PostgreSQL: ``pg_try_advisory_lock`` (released on exit). Any other dialect
    (SQLite): a no-op that always reports acquired (single process).

    :param engine: The SQLAlchemy engine to lock against.
    :param lock_key: A 64-bit advisory-lock key.
    :yields: ``True`` if the lock is held for the block, ``False`` otherwise.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return
    conn = engine.connect()
    try:
        acquired = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                conn.commit()
    finally:
        conn.close()


async def memory_maintenance_loop(
    *,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    lock_key: int = _MEMORY_MAINTENANCE_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` flush + sweep under the lock.

    Resilient — a failed tick is logged and the loop continues; cancellation
    propagates for clean shutdown. The blocking DB work runs in a worker thread
    so the event loop is never blocked.
    """
    from omnigent.runtime import get_memory_store
    from omnigent.stores.memory_store import get_reinforcement_buffer

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            store = get_memory_store()
            buffer = get_reinforcement_buffer()
            with advisory_lock(store.engine, lock_key) as acquired:
                if not acquired:
                    continue
                reinforced, archived = await asyncio.to_thread(
                    run_memory_maintenance_tick, store, buffer
                )
            if reinforced or archived:
                _logger.info(
                    "memory maintenance: reinforced=%d archived=%d", reinforced, archived
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("memory maintenance tick failed: %s", exc, exc_info=True)
