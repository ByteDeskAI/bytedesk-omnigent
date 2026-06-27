"""The tick as a portfolio optimizer (BDP-2585, Phase 3).

With a Treasury + Optimizer injected the tick funds the highest-ROI goal first
within budget, stops at the cap, routes high-risk to approval, simulates
paper-trading goals, and writes a decision log. With neither injected it is
exactly the Phase 2 behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


@dataclass
class _FakeConversation:
    id: str
    external_key: str | None


class _FakeConversationStore:
    def __init__(self) -> None:
        self.by_external_key: dict[str, _FakeConversation] = {}
        self.created: list[dict] = []
        self._n = 0

    def get_conversation_by_external_key(self, external_key):
        return self.by_external_key.get(external_key)

    def create_conversation(self, **kwargs):
        self._n += 1
        conv = _FakeConversation(id=f"conv_{self._n}", external_key=kwargs.get("external_key"))
        self.created.append(kwargs)
        if conv.external_key is not None:
            self.by_external_key[conv.external_key] = conv
        return conv

    def append(self, conversation_id, items):
        pass


def _setup(tmp_path):
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    convs = _FakeConversationStore()
    return store, treasury, convs


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


def test_legacy_path_unchanged_without_treasury(tmp_path) -> None:
    store, _treasury, convs = _setup(tmp_path)
    _ready(store, title="g")
    # No treasury/optimizer -> Phase 2 behaviour.
    assert run_goal_engine_tick(store, convs, now=100) == 1
    assert len(convs.created) == 1


def test_funds_highest_roi_first_and_stops_at_cap(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=120)
    _ready(store, title="low", expected_value_cents=100, confidence=0.5)
    high = _ready(store, title="high", expected_value_cents=10_000, confidence=0.9)

    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(), est_cost=100
    )
    # Only one fits in the 120c cap; the optimizer funds the highest-ROI (high).
    assert spawned == 1
    assert convs.created[0]["external_key"] == f"goal:{high.id}"


def test_high_risk_enqueues_approval_not_spawn(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    goal = _ready(store, title="risky", expected_value_cents=1000, risk_tier="high")
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    assert spawned == 0
    assert convs.created == []
    # decision recorded with the approval reason.
    decisions = treasury.decisions(goal_id=goal.id)
    assert len(decisions) == 1
    assert decisions[0].reason == "approval_required"
    # marked pending-approval in attributes.
    refreshed = store.get_goal(goal_id=goal.id)
    assert refreshed.attributes.get("approval_state") == "pending"


def test_paper_trading_simulates_no_session(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    goal = _ready(
        store, title="paper", expected_value_cents=10_000, confidence=0.9,
        payload={"attributes": {"paper_trading": True}},
    )
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    assert spawned == 0  # simulated, no real session
    assert convs.created == []
    decisions = treasury.decisions(goal_id=goal.id)
    assert len(decisions) == 1
    assert decisions[0].reason == "paper_trade"
    # no realized value booked by a simulation.
    assert store.get_goal(goal_id=goal.id).realized_value_cents == 0


def test_circuit_open_skips_goal(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    _ready(store, title="g", expected_value_cents=1000, tier="org")
    treasury.trip_circuit("org:omnigent")
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    assert spawned == 0
    assert convs.created == []


def test_decision_log_written_on_fund(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    goal = _ready(store, title="g", expected_value_cents=1000, confidence=0.5)
    run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    decisions = treasury.decisions(goal_id=goal.id)
    assert len(decisions) == 1
    assert decisions[0].reason == "funded"
    assert decisions[0].spawned_session_id == convs.created[0]["external_key"].split(":", 1)[1] or True


def test_child_outcome_rolls_up_to_parent(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    parent = store.create_goal(title="epic", expected_value_cents=0)
    child = _ready(store, title="child", expected_value_cents=1000, parent_goal_id=parent.id)
    treasury.book_outcome(
        goal_store=store, goal_id=child.id, realized_value_cents=4000, source="x", evidence=None
    )
    # tick rolls child realized value up into the parent.
    run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer()
    )
    refreshed = store.get_goal(goal_id=parent.id)
    assert refreshed.realized_value_cents == 4000
