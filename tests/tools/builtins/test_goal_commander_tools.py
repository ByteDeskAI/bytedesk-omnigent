"""Command-center commander tools (BDP-2598): happy + gate/failure paths.

Real sqlite goal/treasury stores (no network, no LLM) — the engine-test pattern.
The module-level ``get_goal_store`` / ``get_treasury`` accessors are monkeypatched
so the tools resolve the test stores.
"""
from __future__ import annotations

import json

import pytest

from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.tools.goal_tools import (
    GoalAdjustBudgetTool,
    GoalBatchApproveTool,
    GoalDecomposeTool,
    GoalPrioritizeTool,
    GoalReadDecisionsTool,
    GoalReadFrontierTool,
    GoalReadLedgerTool,
    GoalSetPostureTool,
)
from omnigent.tools.base import ToolContext


@pytest.fixture
def stores(tmp_path, monkeypatch):
    db = f"sqlite:///{tmp_path / 'commander.db'}"
    goals = SqlAlchemyGoalStore(db)
    treasury = SqlAlchemyTreasury(db)
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)
    monkeypatch.setattr("bytedesk_omnigent.engine.treasury.get_treasury", lambda: treasury)
    return {"goals": goals, "treasury": treasury}


def _ctx() -> ToolContext:
    return ToolContext(task_id="t", agent_id="commander", conversation_id="c")


def _call(tool, args: dict) -> dict:
    return json.loads(tool.invoke(json.dumps(args), _ctx()))


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


# ── goal_prioritize ──────────────────────────────────────────────────────────
def test_prioritize_by_order(stores) -> None:
    a = stores["goals"].create_goal(title="a", priority=5)
    b = stores["goals"].create_goal(title="b", priority=5)
    out = _call(GoalPrioritizeTool(), {"goal_ids": [b.id, a.id]})
    by_id = {r["goal_id"]: r["priority"] for r in out["updated"]}
    assert by_id[b.id] == 1 and by_id[a.id] == 2


def test_prioritize_requires_input(stores) -> None:
    assert "error" in _call(GoalPrioritizeTool(), {})


# ── goal_adjust_budget ───────────────────────────────────────────────────────
def test_adjust_budget_by_goal_scope(stores) -> None:
    g = stores["goals"].create_goal(title="g", target_kind="organization", target_id="omnigent")
    out = _call(GoalAdjustBudgetTool(), {"goal_id": g.id, "cap_cents": 5000})
    assert out["scope"] == f"{g.tier}:{g.target_id}"
    assert stores["treasury"].remaining_cents(g.tier, g.target_id) == 5000


def test_adjust_budget_missing_goal(stores) -> None:
    assert "error" in _call(GoalAdjustBudgetTool(), {"goal_id": "nope", "cap_cents": 1})


# ── goal_set_posture (governance gate) ───────────────────────────────────────
def test_set_posture_full_auto_gated_by_default(stores, monkeypatch) -> None:
    monkeypatch.delenv("BYTEDESK_GOALS_ARMING_ENABLED", raising=False)
    out = _call(GoalSetPostureTool(), {"posture": "full_auto"})
    assert out["armed"] is False and "error" in out


def test_set_posture_disarm_always_allowed(stores, monkeypatch) -> None:
    monkeypatch.delenv("BYTEDESK_GOALS_ARMING_ENABLED", raising=False)
    monkeypatch.setattr(
        "bytedesk_omnigent.engine.config.set_autonomy_posture",
        _async_return("gated"),
    )
    out = _call(GoalSetPostureTool(), {"posture": "gated"})
    assert out["posture"] == "gated" and out["armed"] is False


def test_set_posture_full_auto_armed_when_enabled(stores, monkeypatch) -> None:
    monkeypatch.setenv("BYTEDESK_GOALS_ARMING_ENABLED", "1")
    monkeypatch.setattr(
        "bytedesk_omnigent.engine.config.set_autonomy_posture",
        _async_return("full_auto"),
    )
    out = _call(GoalSetPostureTool(), {"posture": "full_auto", "target_id": "acme"})
    assert out["posture"] == "full_auto" and out["armed"] is True


def _async_return(value):
    async def _f(*_a, **_k):
        return value
    return _f


# ── goal_read_frontier ───────────────────────────────────────────────────────
def test_read_frontier_ranks_actionable(stores) -> None:
    low = _ready(stores["goals"], title="low", risk_tier="low", expected_value_cents=1000)
    high = _ready(stores["goals"], title="high", risk_tier="low", expected_value_cents=9000)
    out = _call(GoalReadFrontierTool(), {})
    ids = [r["goal_id"] for r in out["frontier"]]
    assert ids == [high.id, low.id]
    assert out["frontier"][0]["roi"] > 0


def test_read_frontier_empty(stores) -> None:
    assert _call(GoalReadFrontierTool(), {})["frontier"] == []


# ── goal_read_decisions / goal_read_ledger ───────────────────────────────────
def test_read_decisions_and_ledger_empty(stores) -> None:
    assert _call(GoalReadDecisionsTool(), {})["decisions"] == []
    ledger = _call(GoalReadLedgerTool(), {})
    assert ledger["outcomes"] == [] and ledger["realized_value_cents"] == 0


def test_read_ledger_reports_booked_value(stores) -> None:
    g = stores["goals"].create_goal(title="g")
    stores["treasury"].book_outcome(
        goal_store=stores["goals"], goal_id=g.id, realized_value_cents=750,
        source="stripe", evidence=None,
    )
    out = _call(GoalReadLedgerTool(), {"goal_id": g.id})
    assert out["realized_value_cents"] == 750
    assert out["outcomes"][0]["goal_id"] == g.id


# ── goal_batch_approve ───────────────────────────────────────────────────────
def test_batch_approve_activates(stores) -> None:
    a = stores["goals"].create_goal(title="a", readiness_kind="deferred")
    out = _call(GoalBatchApproveTool(), {"goal_ids": [a.id, "missing"]})
    by_id = {r["goal_id"]: r["approved"] for r in out["results"]}
    assert by_id[a.id] is True and by_id["missing"] is False


def test_batch_approve_requires_ids(stores) -> None:
    assert "error" in _call(GoalBatchApproveTool(), {"goal_ids": []})


# ── goal_decompose ───────────────────────────────────────────────────────────
def test_decompose_creates_children(stores) -> None:
    parent = stores["goals"].create_goal(
        title="parent", target_kind="organization", target_id="omnigent"
    )
    out = _call(
        GoalDecomposeTool(),
        {"goal_id": parent.id, "spec": [{"title": "child-1"}, {"title": "child-2"}]},
    )
    assert len(out["children"]) == 2
    assert {c["title"] for c in out["children"]} == {"child-1", "child-2"}


def test_decompose_unknown_parent(stores) -> None:
    assert "error" in _call(GoalDecomposeTool(), {"goal_id": "nope", "spec": [{"title": "x"}]})
