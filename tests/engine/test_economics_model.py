"""Economics on the goal model + migration (BDP-2585, Phase 3).

The goal now carries its own economics — expected/realized value, confidence,
risk tier, tier, parent — so the tick can rank by ROI. These are additive with
defaults, so an existing goal (no economics set) round-trips unchanged.
"""
from __future__ import annotations

from bytedesk_omnigent.goals import SqlAlchemyGoalStore, roi


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_economics_columns_default_unchanged(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="plain goal")

    # Defaults so existing behaviour is untouched.
    assert goal.expected_value_cents == 0
    assert goal.realized_value_cents == 0
    assert goal.confidence == 0.5
    assert goal.risk_tier == "low"
    assert goal.parent_goal_id is None
    # tier derives from target_kind (organization -> org).
    assert goal.tier == "org"
    assert goal.success_condition is None


def test_tier_derives_from_target_kind(tmp_path) -> None:
    store = _store(tmp_path)
    dept = store.create_goal(title="d", target_kind="department", target_id="sales")
    agent = store.create_goal(title="a", target_kind="agent", target_id="maya")
    assert dept.tier == "department"
    assert agent.tier == "agent"


def test_economics_round_trip(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="valuable",
        expected_value_cents=10_000,
        confidence=0.8,
        risk_tier="high",
        tier="department",
        success_condition={"type": "leaf", "sensor": "manual",
                           "query": {}, "predicate": {"op": "exists"}},
    )
    fetched = store.get_goal(goal_id=goal.id)
    assert fetched is not None
    assert fetched.expected_value_cents == 10_000
    assert fetched.confidence == 0.8
    assert fetched.risk_tier == "high"
    assert fetched.tier == "department"
    assert fetched.success_condition == goal.success_condition


def test_update_goal_economics(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="g")
    updated = store.update_goal(
        goal_id=goal.id, expected_value_cents=500, confidence=0.9, risk_tier="medium"
    )
    assert updated is not None
    assert updated.expected_value_cents == 500
    assert updated.confidence == 0.9
    assert updated.risk_tier == "medium"


def test_roi_formula(tmp_path) -> None:
    store = _store(tmp_path)
    # EV 10000c, confidence 0.5, remaining budget 1000c -> (10000*0.5)/1000 = 5.0
    goal = store.create_goal(title="g", expected_value_cents=10_000, confidence=0.5)
    assert roi(goal, remaining_budget_cents=1000) == 5.0
    # remaining budget floored at 1 to avoid div-by-zero.
    assert roi(goal, remaining_budget_cents=0) == 5000.0


def test_attributes_accessor(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="g", payload={"attributes": {"paper_trading": True}})
    assert goal.attributes == {"paper_trading": True}
    plain = store.create_goal(title="p")
    assert plain.attributes == {}
