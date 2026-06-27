"""Frontier read projection (BDP-2598): actionable+ranked + ROI + waiting_reasons."""
from __future__ import annotations

from bytedesk_omnigent.engine.frontier import build_frontier
from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


def test_frontier_ranks_actionable_by_risk_decayed_roi(tmp_path) -> None:
    store = _store(tmp_path)
    low = _ready(store, title="low-ev", risk_tier="low", expected_value_cents=1000)
    high = _ready(store, title="high-ev", risk_tier="low", expected_value_cents=9000)
    rows = build_frontier(goal_store=store, optimizer=RoiOptimizer())
    assert [r["goal_id"] for r in rows] == [high.id, low.id]
    assert all(r["actionable"] for r in rows)
    assert rows[0]["roi"] > 0


def test_frontier_filters_by_target(tmp_path) -> None:
    store = _store(tmp_path)
    org = _ready(store, title="org", target_kind="organization", target_id="omnigent")
    _ready(store, title="dept", target_kind="department", target_id="eng")
    rows = build_frontier(goal_store=store, target_kind="organization")
    assert [r["goal_id"] for r in rows] == [org.id]


def test_frontier_surfaces_waiting_reasons_for_non_actionable(tmp_path) -> None:
    store = _store(tmp_path)
    from bytedesk_omnigent.engine.conditions import All, Leaf, Predicate
    from bytedesk_omnigent.engine.resolver import CONDITION_PAYLOAD_KEY
    from bytedesk_omnigent.engine.sensors import build_default_registry

    upstream = store.create_goal(title="upstream")  # still open -> condition unmet
    tree = All([Leaf("goal_outcome", {"goal_id": upstream.id}, Predicate("equals", "done"))])
    # Ready+owned (so it's a candidate) but its condition tree gates it.
    blocked = store.create_goal(title="blocked", payload={CONDITION_PAYLOAD_KEY: tree.to_dict()})
    store.claim_goal(goal_id=blocked.id, owner_agent_id="maya")

    rows = build_frontier(goal_store=store, sensor_registry=build_default_registry())
    blocked_rows = [r for r in rows if r["goal_id"] == blocked.id]
    assert blocked_rows and blocked_rows[0]["actionable"] is False
    assert blocked_rows[0]["waiting_reasons"]


def test_frontier_empty_when_no_ready_goals(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_goal(title="unclaimed")  # open, not owned -> not a candidate
    assert build_frontier(goal_store=store) == []
