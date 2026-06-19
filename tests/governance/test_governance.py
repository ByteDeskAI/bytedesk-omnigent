"""Tests for the governance read model (BDP-2278 F5 backbone, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.deliberation import SqlAlchemyDeliberationStore
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.governance import governance_summary, outcome_leaderboard
from bytedesk_omnigent.outcomes import SqlAlchemyOutcomeLedger


def _stores(tmp_path):
    db = f"sqlite:///{tmp_path / 'org.db'}"
    return (
        SqlAlchemyGoalStore(db),
        SqlAlchemyDeliberationStore(db),
        SqlAlchemyOutcomeLedger(db),
    )


def test_governance_summary_rolls_up_goals_and_open_deliberations(tmp_path) -> None:
    goals, delib, _ = _stores(tmp_path)
    g1 = goals.create_goal(title="open one", now=100)
    goals.create_goal(title="open two", now=100)
    goals.advance_goal(goal_id=g1.id, status="blocked", now=101)
    delib.start(topic="pricing", proposal="$99", opened_by="ag_ceo", now=100)
    decided = delib.start(topic="hiring", proposal="hire", now=100)
    delib.decide(deliberation_id=decided.id, decision="yes", decided_by="ag_ceo", now=101)

    summary = governance_summary(goal_store=goals, deliberation_store=delib)

    assert summary["goals"]["total"] == 2
    assert summary["goals"]["by_status"] == {"open": 1, "blocked": 1}
    # Only the still-open deliberation is surfaced.
    assert len(summary["open_deliberations"]) == 1
    assert summary["open_deliberations"][0]["topic"] == "pricing"


def test_governance_summary_empty(tmp_path) -> None:
    goals, delib, _ = _stores(tmp_path)
    summary = governance_summary(goal_store=goals, deliberation_store=delib)
    assert summary == {"goals": {"total": 0, "by_status": {}}, "open_deliberations": []}


def test_outcome_leaderboard_ranks_agents(tmp_path) -> None:
    _, _, ledger = _stores(tmp_path)
    ledger.record_outcome(agent_id="ag_a", kind="deal", metric="revenue", value=500, now=100)
    ledger.record_outcome(agent_id="ag_b", kind="deal", metric="revenue", value=900, now=101)

    board = outcome_leaderboard(outcome_ledger=ledger, metric="revenue")

    assert board["metric"] == "revenue"
    assert board["leaderboard"] == [
        {"agent_id": "ag_b", "value": 900.0},
        {"agent_id": "ag_a", "value": 500.0},
    ]
