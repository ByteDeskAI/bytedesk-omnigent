"""Treasury — budgets, exactly-once reservations, the outcome ledger + flywheel
(BDP-2585, Phase 3, ADR-0008 Protocol+default, ADR-0009 exactly-once)."""
from __future__ import annotations

import pytest

from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _stores(tmp_path):
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    return store, treasury


def test_can_fund_with_no_budget_is_true(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g")
    # No budget configured for this scope -> ungated (Phase-2-preserving default).
    assert treasury.can_fund(goal, est_cost=10_000) is True


def test_can_fund_respects_cap(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", target_kind="department", target_id="sales", tier="department")
    treasury.set_budget(tier="department", target_id="sales", cap_cents=1000)
    assert treasury.can_fund(goal, est_cost=500) is True
    assert treasury.can_fund(goal, est_cost=5000) is False


def test_can_fund_respects_inherited_parent_cap(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    parent = store.create_goal(title="org", target_kind="organization", target_id="omnigent", tier="org")
    child = store.create_goal(
        title="child", target_kind="agent", target_id="maya", tier="agent",
        parent_goal_id=parent.id,
    )
    # Child's own scope is uncapped, but the org parent has a tight cap.
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=300)
    assert treasury.can_fund(child, est_cost=200, goal_store=store) is True
    assert treasury.can_fund(child, est_cost=400, goal_store=store) is False


def test_reserve_and_settle_exactly_once(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", tier="org")
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)

    res = treasury.reserve(goal, est_cost=3000)
    assert res is not None
    # Same idempotency key (same goal+period) reserves once.
    res2 = treasury.reserve(goal, est_cost=3000, period_key=res.period_key)
    assert res2 is None  # already reserved this period

    spent = treasury.spent_cents(tier="org", target_id="omnigent")
    assert spent == 3000  # reserve provisionally charges the budget

    treasury.settle(res, actual_cost=1000)
    assert treasury.spent_cents(tier="org", target_id="omnigent") == 1000  # corrected to actual


def test_reserve_denied_over_cap(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", tier="org")
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=1000)
    assert treasury.reserve(goal, est_cost=5000) is None


def test_book_outcome_writes_ledger_and_replenishes(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", tier="org")
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=1000)
    # Burn the budget.
    res = treasury.reserve(goal, est_cost=1000)
    assert res is not None
    assert treasury.can_fund(goal, est_cost=1) is False

    booked = treasury.book_outcome(
        goal_store=store, goal_id=goal.id, realized_value_cents=5000,
        source="stripe", evidence={"invoice": "in_1"},
    )
    assert booked is not None
    # realized value is on the goal now (only book_outcome writes it).
    refreshed = store.get_goal(goal_id=goal.id)
    assert refreshed.realized_value_cents == 5000
    # ledger row exists.
    assert len(treasury.outcomes(goal_id=goal.id)) == 1
    # flywheel: booked revenue refilled the tier budget -> can fund again.
    assert treasury.can_fund(goal, est_cost=1000) is True


def test_circuit_open_global_switch(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", tier="org")
    assert treasury.circuit_open("org:omnigent") is False
    treasury.trip_circuit("org:omnigent")
    assert treasury.circuit_open("org:omnigent") is True
    treasury.reset_circuit("org:omnigent")
    assert treasury.circuit_open("org:omnigent") is False


def test_circuit_open_on_burn_with_zero_realized(tmp_path) -> None:
    store, treasury = _stores(tmp_path)
    goal = store.create_goal(title="g", tier="org")
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000, anomaly_threshold_cents=2000)
    # Burn well past the anomaly threshold with zero realized value booked.
    treasury.reserve(goal, est_cost=5000)
    assert treasury.circuit_open("org:omnigent") is True
    # A booked outcome clears the anomaly.
    treasury.book_outcome(
        goal_store=store, goal_id=goal.id, realized_value_cents=6000, source="x", evidence=None
    )
    assert treasury.circuit_open("org:omnigent") is False
