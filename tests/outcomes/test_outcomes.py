"""Tests for the Business Outcome Ledger: append + scoreboard rollup
(BDP-2268 B7, ADR-0142)."""
from __future__ import annotations

from omnigent.goals import SqlAlchemyGoalStore
from omnigent.outcomes import SqlAlchemyOutcomeLedger


def _stores(tmp_path) -> tuple[SqlAlchemyOutcomeLedger, SqlAlchemyGoalStore]:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    return SqlAlchemyOutcomeLedger(db), SqlAlchemyGoalStore(db)


def test_record_outcome_appends_and_rolls_cumulative_into_scoreboard(tmp_path) -> None:
    ledger, goals = _stores(tmp_path)

    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=500, now=100)
    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=300, now=200)

    # Append-only ledger keeps both rows.
    assert len(ledger.list_outcomes(agent_id="ag_a")) == 2
    # The scoreboard reflects the CUMULATIVE total, so find-specialist ranks by it.
    board = goals.scoreboard(metric="revenue")
    assert board == [("ag_a", 800.0)]


def test_leaderboard_ranks_agents_by_cumulative_value(tmp_path) -> None:
    ledger, _ = _stores(tmp_path)

    def resolve(agent: str, when: int) -> None:
        ledger.record_outcome(
            agent_id=agent, kind="ticket_resolved", metric="tickets", value=1, now=when
        )

    resolve("ag_a", 100)
    resolve("ag_b", 101)
    resolve("ag_b", 102)

    assert ledger.leaderboard(metric="tickets") == [("ag_b", 2.0), ("ag_a", 1.0)]


def test_list_outcomes_filters_by_kind(tmp_path) -> None:
    ledger, _ = _stores(tmp_path)
    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=1, now=100)
    ledger.record_outcome(
        agent_id="ag_a", kind="feature_shipped", metric="ships", value=1, now=101
    )

    shipped = ledger.list_outcomes(kind="feature_shipped")
    assert len(shipped) == 1
    assert shipped[0].metric == "ships"
