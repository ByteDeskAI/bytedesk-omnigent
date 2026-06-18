"""Tests for the native cron scheduler: due detection + exactly-once claim
(BDP-2250, ADR-0142)."""
from __future__ import annotations

import time

from omnigent.scheduler import (
    SqlAlchemyCronScheduler,
    compute_next_fire,
    run_cron_scheduler_tick,
)


def _sched(tmp_path) -> SqlAlchemyCronScheduler:
    return SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")


def test_due_trigger_is_claimed_once_then_not_due(tmp_path) -> None:
    sched = _sched(tmp_path)
    now = int(time.time())

    trig = sched.register_trigger(
        agent_id="maya",
        key="standup",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=now,
        payload={"message": "standup time"},
        now=now,
    )
    # The trigger is due now.
    assert [t.id for t in sched.due_triggers(now=now)] == [trig.id]

    nxt = compute_next_fire("interval", "60", now)
    assert nxt == now + 60

    # First claim of this fire instant wins; a second claim of the SAME instant
    # is an idempotent no-op (exactly-once firing).
    assert sched.claim_fire(
        trigger_id=trig.id, expected_next_fire_at=now, new_next_fire_at=nxt, now=now
    ) is True
    assert sched.claim_fire(
        trigger_id=trig.id, expected_next_fire_at=now, new_next_fire_at=nxt, now=now
    ) is False

    # No longer due — next_fire_at advanced to now + 60.
    assert sched.due_triggers(now=now) == []
    assert [t.id for t in sched.due_triggers(now=now + 60)] == [trig.id]


def test_register_trigger_is_idempotent_by_agent_and_key(tmp_path) -> None:
    sched = _sched(tmp_path)
    now = int(time.time())
    a = sched.register_trigger(
        agent_id="maya", key="standup", schedule_kind="interval",
        schedule_expr="60", next_fire_at=now, now=now,
    )
    # Re-register same (agent, key): updates in place, no duplicate row.
    b = sched.register_trigger(
        agent_id="maya", key="standup", schedule_kind="interval",
        schedule_expr="120", next_fire_at=now + 5, now=now,
    )
    assert a.id == b.id
    assert b.schedule_expr == "120"
    assert len(sched.due_triggers(now=now + 10)) == 1


def test_tick_claims_and_dispatches_only_due_triggers(tmp_path) -> None:
    sched = _sched(tmp_path)
    now = int(time.time())
    sched.register_trigger(
        agent_id="maya", key="standup", schedule_kind="interval",
        schedule_expr="60", next_fire_at=now, payload={"message": "hi"}, now=now,
    )
    sched.register_trigger(  # not due until now + 1000
        agent_id="caleb", key="review", schedule_kind="interval",
        schedule_expr="300", next_fire_at=now + 1000, now=now,
    )

    fired: list[str] = []
    n = run_cron_scheduler_tick(sched, lambda t: fired.append(t.agent_id), now=now)
    assert n == 1
    assert fired == ["maya"]

    # A second tick at the same instant fires nothing (maya advanced, caleb future).
    assert run_cron_scheduler_tick(sched, lambda t: fired.append(t.agent_id), now=now) == 0
    assert fired == ["maya"]
