"""Unit tests for the native org tools over the durable social/why-act/decision
stores (BDP-2262 C2/C3/C6/B7 integration, ADR-0142). Verifies the round-trips +
the server-side agent-identity stamping (anti-spoofing)."""
from __future__ import annotations

import json

import pytest

from omnigent.deliberation import SqlAlchemyDeliberationStore
from omnigent.goals import SqlAlchemyGoalStore
from omnigent.outcomes import SqlAlchemyOutcomeLedger
from omnigent.peer import SqlAlchemyPeerMessageStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.deliberation_tools import (
    DeliberationDecideTool,
    DeliberationFindTool,
    DeliberationPositionTool,
    DeliberationStartTool,
)
from omnigent.tools.builtins.goal_tools import (
    GoalAdvanceTool,
    GoalClaimTool,
    GoalCreateTool,
    GoalListTool,
)
from omnigent.tools.builtins.outcome_tools import OutcomeRecordTool
from omnigent.tools.builtins.peer_tools import PeerInboxTool, PeerSendTool


@pytest.fixture
def stores(tmp_path, monkeypatch):
    db = f"sqlite:///{tmp_path / 'org.db'}"
    peer = SqlAlchemyPeerMessageStore(db)
    goals = SqlAlchemyGoalStore(db)
    delib = SqlAlchemyDeliberationStore(db)
    ledger = SqlAlchemyOutcomeLedger(db)
    monkeypatch.setattr("omnigent.peer.get_peer_message_store", lambda: peer)
    monkeypatch.setattr("omnigent.goals.get_goal_store", lambda: goals)
    monkeypatch.setattr("omnigent.deliberation.get_deliberation_store", lambda: delib)
    monkeypatch.setattr("omnigent.outcomes.get_outcome_ledger", lambda: ledger)
    return {"peer": peer, "goals": goals, "delib": delib, "ledger": ledger}


def _ctx(agent_id: str = "ag_a") -> ToolContext:
    return ToolContext(task_id="t", agent_id=agent_id, conversation_id="c")


def _call(tool, args: dict, agent_id: str = "ag_a") -> dict:
    return json.loads(tool.invoke(json.dumps(args), _ctx(agent_id)))


def test_peer_send_then_inbox(stores) -> None:
    out = _call(PeerSendTool(), {"topic": "hi", "body": "hello", "to_agent": "ag_b"})
    assert out["seq"] >= 1
    inbox = _call(PeerInboxTool(), {}, agent_id="ag_b")
    assert len(inbox["messages"]) == 1
    assert inbox["messages"][0]["from"] == "ag_a"
    assert inbox["messages"][0]["body"] == "hello"


def test_peer_send_stamps_sender_from_context_not_args(stores) -> None:
    # A forged 'from_agent' arg is ignored — the sender is the ctx agent.
    _call(
        PeerSendTool(),
        {"topic": "t", "body": "b", "from_agent": "ag_forged", "to_agent": "ag_b"},
        agent_id="ag_real",
    )
    inbox = _call(PeerInboxTool(), {}, agent_id="ag_b")
    assert inbox["messages"][0]["from"] == "ag_real"


def test_goal_create_list_claim_advance(stores) -> None:
    gid = _call(GoalCreateTool(), {"title": "ship feature"})["goal_id"]
    assert any(g["goal_id"] == gid for g in _call(GoalListTool(), {"status": "open"})["goals"])

    assert _call(GoalClaimTool(), {"goal_id": gid}, agent_id="ag_b")["claimed"] is True
    # A second claim by another agent loses the guarded race.
    assert _call(GoalClaimTool(), {"goal_id": gid}, agent_id="ag_c")["claimed"] is False

    advanced = _call(GoalAdvanceTool(), {"goal_id": gid, "status": "in_progress"})
    assert advanced["status"] == "in_progress"


def test_deliberation_start_position_decide_find(stores) -> None:
    did = _call(
        DeliberationStartTool(), {"topic": "pricing", "proposal": "$99"}, agent_id="ag_ceo"
    )["deliberation_id"]
    _call(
        DeliberationPositionTool(),
        {"deliberation_id": did, "stance": "against", "body": "churn"},
        agent_id="ag_b",
    )
    assert _call(
        DeliberationDecideTool(),
        {"deliberation_id": did, "decision": "$89"},
        agent_id="ag_ceo",
    )["decided"] is True
    assert _call(DeliberationFindTool(), {"topic": "pricing"})["decision"] == "$89"


def test_outcome_record_rolls_into_scoreboard(stores) -> None:
    out = _call(
        OutcomeRecordTool(), {"kind": "deal_won", "metric": "revenue", "value": 500}
    )
    assert out["metric"] == "revenue"
    assert stores["goals"].scoreboard(metric="revenue") == [("ag_a", 500.0)]
