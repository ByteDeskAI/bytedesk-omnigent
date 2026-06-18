"""Tests for the durable signal/await bus: deliver-by-id idempotency
(BDP-2248, ADR-0142, aligned ADR-0009)."""
from __future__ import annotations

import time

from omnigent.bus import DeliveryStatus, SqlAlchemySignalBus


def _bus(tmp_path) -> SqlAlchemySignalBus:
    # Mirrors tests/runtime/test_memory_maintenance.py::_store — SQLite on tmp_path.
    return SqlAlchemySignalBus(f"sqlite:///{tmp_path / 'bus.db'}")


def test_deliver_to_registered_wait_returns_delivered_then_second_deliver_already_resolved(
    tmp_path,
) -> None:
    bus = _bus(tmp_path)
    now = int(time.time())
    sid = "run-abc:node-7"  # raw {runId}:{nodeId} colon form, kept unescaped

    bus.register_wait(
        signal_id=sid,
        session_id="sess-1",
        key="subscribe:teamcity",
        kind="subscribe",
        target="teamcity",
        now=now,
    )
    assert [
        w.signal_id for w in bus.list_pending(kind="subscribe", target="teamcity")
    ] == [sid]

    # First deliver: pending wait resolves, waiter is targeted.
    first = bus.deliver(signal_id=sid, payload={"build": "green"}, now=now + 1)
    assert first.status is DeliveryStatus.DELIVERED
    assert first.session_id == "sess-1"
    assert first.payload == {"build": "green"}
    assert bus.list_pending(kind="subscribe", target="teamcity") == []  # no longer pending

    # The wake payload is durably queued exactly once for the parked session.
    inbox = bus.drain_inbox(session_id="sess-1")
    assert len(inbox) == 1
    assert inbox[0]["payload"]["build"] == "green"

    # Second deliver of the SAME signal_id: idempotent replay -> AlreadyResolved.
    second = bus.deliver(signal_id=sid, payload={"build": "green"}, now=now + 2)
    assert second.status is DeliveryStatus.ALREADY_RESOLVED
    # And no duplicate wake row was written.
    assert bus.drain_inbox(session_id="sess-1") == []


def test_deliver_unmatched_signal_is_dead_lettered(tmp_path) -> None:
    """An unmatched deliver (no registered wait) dead-letters instead of
    succeeding silently — the route maps this to 404, never 2xx (BDP-1419)."""
    bus = _bus(tmp_path)
    result = bus.deliver(signal_id="never-registered:node-1", payload={"x": 1})
    assert result.status is DeliveryStatus.DEAD_LETTERED
    assert result.session_id is None
    # A dead-letter row is recorded but is never drained into a session inbox.
    assert bus.drain_inbox(session_id="sess-1") == []


def test_registered_wait_and_wake_survive_a_process_restart(tmp_path) -> None:
    """The durable wake survives a runner/process restart: a brand-new bus on
    the same database drains the wake exactly once (closes orphaned_after_restart
    for this primitive)."""
    db = f"sqlite:///{tmp_path / 'bus.db'}"
    now = int(time.time())

    bus = SqlAlchemySignalBus(db)
    bus.register_wait(
        signal_id="r:n", session_id="sess-9", key="subscribe:x",
        kind="subscribe", target="x", now=now,
    )
    assert (
        bus.deliver(signal_id="r:n", payload={"ok": True}, now=now + 1).status
        is DeliveryStatus.DELIVERED
    )

    # Simulate a restart: a fresh bus instance over the SAME durable database.
    fresh = SqlAlchemySignalBus(db)
    inbox = fresh.drain_inbox(session_id="sess-9")
    assert len(inbox) == 1
    assert inbox[0]["payload"]["ok"] is True
    # The delivered_at latch persists, so a re-drain after reconnect is empty.
    assert fresh.drain_inbox(session_id="sess-9") == []
