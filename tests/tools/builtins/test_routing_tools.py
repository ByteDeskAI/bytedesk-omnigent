"""Tests for the self-learning routing tool ``find_specialist`` (BDP-2276 E2, ADR-0142).

The scoreboard the tool ranks by is upserted by the Business Outcome Ledger
(``outcome_record`` → ``omnigent/outcomes.py``), so ranking *learns* from recorded
outcomes: the more an agent delivers on a metric, the higher it ranks.
"""

from __future__ import annotations

import json

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.outcomes import SqlAlchemyOutcomeLedger
from bytedesk_omnigent.tools.routing_tools import FindSpecialistTool
from omnigent.tools.base import ToolContext


def _ctx() -> ToolContext:
    # find_specialist is read-only and ignores ctx; supply the required fields.
    return ToolContext(task_id="t", agent_id="ag_caller", conversation_id="conv_1")


def test_find_specialist_ranks_by_recorded_outcomes(tmp_path, monkeypatch) -> None:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    ledger = SqlAlchemyOutcomeLedger(db)
    goals = SqlAlchemyGoalStore(db)
    # ag_b delivered more revenue than ag_a → it must rank first.
    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=100, now=1)
    ledger.record_outcome(agent_id="ag_b", kind="deal_won", metric="revenue", value=900, now=2)

    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)

    out = json.loads(FindSpecialistTool().invoke(json.dumps({"metric": "revenue"}), _ctx()))
    assert [c["agent_id"] for c in out["candidates"]] == ["ag_b", "ag_a"]
    assert out["candidates"][0]["score"] == 900.0


def test_find_specialist_learns_after_new_outcome(tmp_path, monkeypatch) -> None:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    ledger = SqlAlchemyOutcomeLedger(db)
    goals = SqlAlchemyGoalStore(db)
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)

    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=100, now=1)
    ledger.record_outcome(agent_id="ag_b", kind="deal_won", metric="revenue", value=50, now=2)
    first = json.loads(FindSpecialistTool().invoke(json.dumps({"metric": "revenue"}), _ctx()))
    assert first["candidates"][0]["agent_id"] == "ag_a"

    # ag_b delivers a big win → the scoreboard, and thus routing, learns.
    ledger.record_outcome(agent_id="ag_b", kind="deal_won", metric="revenue", value=500, now=3)
    after = json.loads(FindSpecialistTool().invoke(json.dumps({"metric": "revenue"}), _ctx()))
    assert after["candidates"][0]["agent_id"] == "ag_b"


def test_find_specialist_unknown_metric_returns_empty(tmp_path, monkeypatch) -> None:
    goals = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'org.db'}")
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)
    out = json.loads(FindSpecialistTool().invoke(json.dumps({"metric": "nope"}), _ctx()))
    assert out["candidates"] == []
    assert "No recorded outcomes" in out["message"]


def test_find_specialist_requires_metric() -> None:
    out = json.loads(FindSpecialistTool().invoke(json.dumps({}), _ctx()))
    assert "error" in out


def test_find_specialist_clamps_limit(tmp_path, monkeypatch) -> None:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    ledger = SqlAlchemyOutcomeLedger(db)
    goals = SqlAlchemyGoalStore(db)
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)
    for i in range(3):
        ledger.record_outcome(
            agent_id=f"ag_{i}", kind="deal_won", metric="revenue", value=i + 1, now=i
        )
    out = json.loads(
        FindSpecialistTool().invoke(json.dumps({"metric": "revenue", "limit": 1}), _ctx())
    )
    assert len(out["candidates"]) == 1
