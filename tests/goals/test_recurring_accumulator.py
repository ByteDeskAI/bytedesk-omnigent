"""Recurring progress accumulator + until_done heartbeat (BDP-2596 Wave 3, 3+4).

A recurring goal accumulates progress toward a standing metric across fires
(``payload.progress`` = {current, target}); it never closes. An until_done goal
re-spawns each heartbeat until its success_condition trips, then completes.
Storage: payload (no migration). Fakes only.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.progress import (
    accumulate_progress,
    progress_view,
)
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path):
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")


class _FakeScheduler:
    def __init__(self):
        self.triggers = []

    def register_trigger(self, **kw):
        self.triggers.append(kw)


def test_accumulate_progress_sums_deltas_across_fires(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="lead gen", cadence_kind="recurring", cadence_expr="0 9 * * *",
        payload={"progress": {"current": 0, "target": 100}},
        scheduler=_FakeScheduler(),
    )
    accumulate_progress(store, goal_id=goal.id, delta=10)
    accumulate_progress(store, goal_id=goal.id, delta=15)
    view = progress_view(store.get_goal(goal_id=goal.id))
    assert view["current"] == 25
    assert view["target"] == 100
    assert view["remaining"] == 75


def test_accumulate_progress_never_closes_recurring_goal(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="metric", cadence_kind="recurring", cadence_expr="0 9 * * *",
        payload={"progress": {"current": 90, "target": 100}},
        scheduler=_FakeScheduler(),
    )
    accumulate_progress(store, goal_id=goal.id, delta=50)  # past target
    refreshed = store.get_goal(goal_id=goal.id)
    assert str(refreshed.status) == "open"  # still standing
    assert progress_view(refreshed)["current"] == 140  # accumulates past target


def test_progress_view_defaults_for_goal_without_accumulator(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="plain")
    assert progress_view(goal) is None  # no accumulator → no view
