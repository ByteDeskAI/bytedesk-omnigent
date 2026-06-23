"""Tests for the native cron scheduler: due detection + exactly-once claim
(BDP-2250, ADR-0142)."""
from __future__ import annotations

import time

import pytest

from bytedesk_omnigent.scheduler import (
    SqlAlchemyCronScheduler,
    compute_next_fire,
    run_cron_scheduler_tick,
)


def _sched(tmp_path) -> SqlAlchemyCronScheduler:
    return SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")


# ── If-Match / optimistic concurrency (BDP-2412, ADR-0150) ────────────────────


def _reg(sched, *, expr="60", expected_version=None):
    return sched.register_trigger(
        agent_id="a",
        key="k",
        schedule_kind="interval",
        schedule_expr=expr,
        expected_version=expected_version,
    )


def test_register_new_trigger_starts_at_version_one(tmp_path) -> None:
    assert _reg(_sched(tmp_path)).version == 1


def test_reregister_matching_version_bumps(tmp_path) -> None:
    sched = _sched(tmp_path)
    _reg(sched)
    updated = _reg(sched, expr="120", expected_version=1)
    assert updated.version == 2 and updated.schedule_expr == "120"


def test_reregister_stale_version_raises_no_clobber(tmp_path) -> None:
    from omnigent.errors import StaleWriteError

    sched = _sched(tmp_path)
    _reg(sched)
    _reg(sched, expr="120", expected_version=1)  # -> v2
    with pytest.raises(StaleWriteError):
        _reg(sched, expr="999", expected_version=1)  # stale
    assert [t.schedule_expr for t in sched.due_triggers(now=2**31)] == ["120"]


def test_reregister_without_version_is_unconditional_and_bumps(tmp_path) -> None:
    sched = _sched(tmp_path)
    _reg(sched)
    assert _reg(sched, expr="120").version == 2  # back-compat


def test_new_trigger_ignores_expected_version(tmp_path) -> None:
    # a precondition on a not-yet-existing (agent,key) is ignored — INSERT, no 412
    t = _sched(tmp_path).register_trigger(
        agent_id="a",
        key="brand_new",
        schedule_kind="interval",
        schedule_expr="60",
        expected_version=5,
    )
    assert t.version == 1


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
