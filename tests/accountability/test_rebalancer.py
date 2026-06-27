"""Economic rebalancer (BDP-2596 Wave 3, feature 2).

The accountability tick ALSO reallocates budget by ROI: it pulls remaining
budget from stalled / negative-ROI scopes and redeploys headroom to high-ROI
scopes, recorded as treasury decisions. Reopen/escalate behaviour is preserved.
Idempotent under the existing advisory lock. Fakes only.
"""
from __future__ import annotations

from bytedesk_omnigent.accountability import run_accountability_tick
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore


def _stores(tmp_path):
    db = f"sqlite:///{tmp_path / 'org.db'}"
    return (
        SqlAlchemyGoalStore(db),
        SqlAlchemyPeerMessageStore(db),
        SqlAlchemyTreasury(db),
    )


def test_rebalance_without_treasury_is_unchanged(tmp_path) -> None:
    goals, peers, _t = _stores(tmp_path)
    goal = goals.create_goal(title="x", now=100)
    goals.claim_goal(goal_id=goal.id, owner_agent_id="a", now=100)
    report = run_accountability_tick(goals, peers, stall_seconds=3600, now=100 + 3601)
    # Behaviour-preserving: no treasury → just reopen/escalate.
    assert report.rebalanced == 1
    assert getattr(report, "reallocated_cents", 0) == 0


def test_pulls_budget_from_stalled_scope_to_high_roi_scope(tmp_path) -> None:
    goals, peers, treasury = _stores(tmp_path)
    # A stalled department scope with idle headroom, and a busy high-ROI dept.
    treasury.set_budget(tier="department", target_id="dept_idle", cap_cents=1000)
    treasury.set_budget(tier="department", target_id="dept_hot", cap_cents=1000)

    # dept_idle has a stalled goal (no realized value) → its headroom is harvested.
    idle = goals.create_goal(
        title="idle", target_kind="department", target_id="dept_idle",
        target_label="Idle", expected_value_cents=100, confidence=0.1, now=100,
    )
    goals.claim_goal(goal_id=idle.id, owner_agent_id="a", now=100)

    # dept_hot has a high-ROI ready goal that wants more budget.
    hot = goals.create_goal(
        title="hot", target_kind="department", target_id="dept_hot",
        target_label="Hot", expected_value_cents=50_000, confidence=0.9, now=100,
    )
    goals.claim_goal(goal_id=hot.id, owner_agent_id="b", now=100)

    before_hot = treasury.remaining_cents("department", "dept_hot")
    report = run_accountability_tick(
        goals, peers, treasury=treasury, stall_seconds=3600, now=100 + 3601,
    )
    after_hot = treasury.remaining_cents("department", "dept_hot")

    assert report.reallocated_cents > 0
    assert after_hot > before_hot  # headroom redeployed to the hot scope
    # The decision is recorded for replay.
    decisions = treasury.decisions()
    assert any(d.reason == "rebalance_redeploy" for d in decisions)


def test_rebalance_is_idempotent(tmp_path) -> None:
    goals, peers, treasury = _stores(tmp_path)
    treasury.set_budget(tier="department", target_id="dept_idle", cap_cents=1000)
    treasury.set_budget(tier="department", target_id="dept_hot", cap_cents=1000)
    idle = goals.create_goal(
        title="idle", target_kind="department", target_id="dept_idle",
        expected_value_cents=100, confidence=0.1, now=100,
    )
    goals.claim_goal(goal_id=idle.id, owner_agent_id="a", now=100)
    hot = goals.create_goal(
        title="hot", target_kind="department", target_id="dept_hot",
        expected_value_cents=50_000, confidence=0.9, now=100,
    )
    goals.claim_goal(goal_id=hot.id, owner_agent_id="b", now=100)

    run_accountability_tick(goals, peers, treasury=treasury, now=100 + 3601)
    cap_after_first = treasury.remaining_cents("department", "dept_hot")
    # Second tick: the idle scope was already harvested (stalled goal reopened) →
    # no further redeploy.
    report2 = run_accountability_tick(goals, peers, treasury=treasury, now=100 + 7201)
    cap_after_second = treasury.remaining_cents("department", "dept_hot")
    assert cap_after_second == cap_after_first
    assert report2.reallocated_cents == 0
