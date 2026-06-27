"""Goal cadence → cron-trigger registration on create (BDP-2583)."""
from __future__ import annotations

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.scheduler.scheduler import SqlAlchemyCronScheduler


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _scheduler(tmp_path) -> SqlAlchemyCronScheduler:
    return SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'goals.db'}")


def test_default_cadence_is_immediate_no_trigger(tmp_path) -> None:
    store = _store(tmp_path)
    scheduler = _scheduler(tmp_path)
    goal = store.create_goal(title="ship release", scheduler=scheduler)
    assert goal.cadence_kind == "immediate"
    assert goal.cadence_expr is None
    assert scheduler.list_triggers() == []


def test_recurring_goal_registers_cron_trigger(tmp_path) -> None:
    store = _store(tmp_path)
    scheduler = _scheduler(tmp_path)
    goal = store.create_goal(
        title="hourly sweep",
        cadence_kind="recurring",
        cadence_expr="0 * * * *",
        scheduler=scheduler,
    )
    assert goal.cadence_kind == "recurring"
    assert goal.cadence_expr == "0 * * * *"

    triggers = scheduler.list_triggers()
    assert len(triggers) == 1
    trig = triggers[0]
    assert trig.schedule_kind.value == "cron"
    assert trig.schedule_expr == "0 * * * *"
    assert trig.payload == {"goal_id": goal.id, "agent_id": None, "kind": "goal"}


def test_until_done_goal_registers_cron_trigger(tmp_path) -> None:
    store = _store(tmp_path)
    scheduler = _scheduler(tmp_path)
    goal = store.create_goal(
        title="nag until shipped",
        cadence_kind="until_done",
        cadence_expr="*/30 * * * *",
        scheduler=scheduler,
    )
    triggers = scheduler.list_triggers()
    assert len(triggers) == 1
    assert triggers[0].payload["kind"] == "goal"
    assert triggers[0].payload["goal_id"] == goal.id


def test_recurring_without_expr_is_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        store.create_goal(title="bad", cadence_kind="recurring", scheduler=_scheduler(tmp_path))
    except ValueError as exc:
        assert "cadence_expr" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for recurring without cadence_expr")
