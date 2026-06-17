"""Out-of-band reinforcement buffer for the agent memory plane (FU1 T8, ADR-0132).

Recall must not take a write lock on the read path, so ``memory_query`` records
the ids it surfaced into this in-process buffer instead of writing the database
inline. A periodic flush (wired into the server's decay/sweep lifespan loop,
T9) drains the buffer and issues a single batched
:meth:`SqlAlchemyMemoryStore.reinforce` UPDATE. The buffer dedupes within a
flush window and rate-limits per row (at most once per ``min_interval_seconds``)
so a hot memory recalled many times is reinforced at most once per window.

Ordering note (ADR-0132): the lifespan loop flushes this buffer **before**
running the decay sweep, so a memory actively being recalled has its clock reset
before the sweep evaluates it for eviction — avoiding archiving a hot row whose
reinforcement is still buffered.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from omnigent.db.utils import now_epoch


class ReinforcementBuffer:
    """Thread-safe dedup + rate-limited buffer of recalled memory ids."""

    def __init__(self, *, min_interval_seconds: int = 60) -> None:
        """
        :param min_interval_seconds: Minimum seconds between reinforcements of
            the same memory; recalls inside this window are dropped.
        """
        self._min_interval = min_interval_seconds
        self._pending: set[str] = set()
        self._last_flushed: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, memory_ids: Iterable[str], *, now: int) -> None:
        """Record recalled memory ids for later reinforcement.

        Dedupes within the pending set and skips ids reinforced within
        ``min_interval_seconds``. In-memory only — performs no database write,
        so the recall path stays a pure read.

        :param memory_ids: Ids surfaced by a recall.
        :param now: Current epoch seconds.
        """
        with self._lock:
            for mid in memory_ids:
                if not mid:
                    continue
                last = self._last_flushed.get(mid)
                if last is not None and (now - last) < self._min_interval:
                    continue
                self._pending.add(mid)

    def pending_count(self) -> int:
        """:returns: The number of distinct ids awaiting reinforcement."""
        with self._lock:
            return len(self._pending)

    def flush(self, store, *, now: int | None = None) -> int:
        """Drain the buffer and reinforce the drained ids in one batched UPDATE.

        :param store: The :class:`SqlAlchemyMemoryStore` to reinforce against.
        :param now: Current epoch seconds; defaults to :func:`now_epoch`.
        :returns: The number of rows reinforced.
        """
        now = now if now is not None else now_epoch()
        with self._lock:
            ids = list(self._pending)
            self._pending.clear()
        if not ids:
            return 0
        count = store.reinforce(ids, now=now)
        with self._lock:
            for mid in ids:
                self._last_flushed[mid] = now
        return count


_BUFFER: ReinforcementBuffer | None = None


def get_reinforcement_buffer() -> ReinforcementBuffer:
    """Return the process-global reinforcement buffer (lazily created)."""
    global _BUFFER
    if _BUFFER is None:
        _BUFFER = ReinforcementBuffer()
    return _BUFFER
