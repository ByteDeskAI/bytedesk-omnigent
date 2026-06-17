"""Recall observability tests (BDP-2147 T13, ADR-0132).

Recall emits a structured stats line so decay/floor behavior is observable and
falsifiable from logs, not guessed.
"""

from __future__ import annotations

import logging
import time

from omnigent.stores.memory_store import SqlAlchemyMemoryStore

_LOGGER = "omnigent.stores.memory_store.sqlalchemy_store"


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")


class _Capture(logging.Handler):
    """Capture records straight off the target logger (omnigent's conftest
    disables propagation, so pytest's root-based caplog can't see them)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_query_logs_recall_stats(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    # One fresh (recalled) + one decayed-below-floor (dropped) memory.
    store.append(scope="topic", owner="shared", name="t", content="alpha fresh", half_life_seconds=100, now=base)
    store.append(scope="topic", owner="shared", name="t", content="alpha stale", half_life_seconds=100, now=base - 10_000)

    logger = logging.getLogger(_LOGGER)
    handler = _Capture()
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        hits = store.query(scope="topic", owner="shared", name="t", query="alpha", now=base)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    assert len(hits) == 1  # the stale one decayed below read_floor
    line = next((m for m in handler.messages if "memory_query" in m), None)
    assert line is not None, "recall must emit a memory_query stats line"
    assert "candidates=2" in line
    assert "dropped_sub_floor=1" in line
    assert "returned=1" in line
