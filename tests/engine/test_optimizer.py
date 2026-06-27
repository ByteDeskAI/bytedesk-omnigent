"""ROI optimizer — rank the actionable frontier (BDP-2585, Phase 3)."""
from __future__ import annotations

from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_rank_by_roi_desc(tmp_path) -> None:
    store = _store(tmp_path)
    low = store.create_goal(title="low", expected_value_cents=100, confidence=0.5)
    high = store.create_goal(title="high", expected_value_cents=10_000, confidence=0.9)
    mid = store.create_goal(title="mid", expected_value_cents=1000, confidence=0.5)

    opt = RoiOptimizer()
    ranked = opt.rank([low, high, mid], now=1000)
    assert [g.id for g in ranked] == [high.id, mid.id, low.id]


def test_rank_risk_decay(tmp_path) -> None:
    store = _store(tmp_path)
    # Same EV/confidence, different risk -> low risk ranks above high risk.
    safe = store.create_goal(title="safe", expected_value_cents=1000, confidence=0.8, risk_tier="low")
    risky = store.create_goal(title="risky", expected_value_cents=1000, confidence=0.8, risk_tier="high")
    opt = RoiOptimizer()
    ranked = opt.rank([risky, safe], now=1000)
    assert [g.id for g in ranked] == [safe.id, risky.id]


def test_rank_stable_for_equal_roi(tmp_path) -> None:
    store = _store(tmp_path)
    a = store.create_goal(title="a", expected_value_cents=0)  # EV 0 -> roi 0
    b = store.create_goal(title="b", expected_value_cents=0)
    opt = RoiOptimizer()
    ranked = opt.rank([a, b], now=1000)
    # Deterministic tie-break (priority then created_at then id) keeps input order.
    assert {g.id for g in ranked} == {a.id, b.id}
