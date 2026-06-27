"""Arbitration wired into the portfolio tick (BDP-2597, Wave 4).

Two ready goals owned by the same actor contend for that actor: with arbitration
ON only the higher tier×priority×ROI goal funds (the other waits with a reason);
with arbitration OFF both fund in ROI order (legacy). Exactly-once funding holds.

Fakes only, no network/LLM.
"""
from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.engine.config import GoalEngineConfig
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
    return SqlAlchemyGoalStore(loc), SqlAlchemyTreasury(loc), _FakeConversationStore()


def _ready(store, **kw):
    g = store.create_goal(**kw)
    store.claim_goal(goal_id=g.id, owner_agent_id="alice")
    return g


def test_arbitration_off_funds_both_in_roi_order(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)
    _ready(store, title="hi", expected_value_cents=1000, confidence=1.0)
    _ready(store, title="lo", expected_value_cents=10, confidence=1.0)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(), est_cost=100,
    )
    assert spawned == 2  # legacy: both same-actor goals fund


def test_arbitration_on_funds_one_and_other_waits(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)
    hi = _ready(store, title="hi", expected_value_cents=1000, confidence=1.0)
    lo = _ready(store, title="lo", expected_value_cents=10, confidence=1.0)
    cfg = GoalEngineConfig(arbitration_enabled=True)
    spawned = run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert spawned == 1  # only the arbitration winner funds
    # The winner is the higher-ROI goal; the loser carries a waiting_reason.
    assert store.get_goal(goal_id=hi.id).attributes.get("waiting_reason") is None
    loser = store.get_goal(goal_id=lo.id)
    assert loser.attributes.get("waiting_reason")
    # The loser was NOT spawned (no double-spawn).
    assert convs.created[0]["external_key"] == f"goal:{hi.id}"


def test_arbitration_loser_funds_next_tick_when_uncontended(tmp_path) -> None:
    # After the winner completes, the loser no longer contends and funds.
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)
    hi = _ready(store, title="hi", expected_value_cents=1000, confidence=1.0)
    lo = _ready(store, title="lo", expected_value_cents=10, confidence=1.0)
    cfg = GoalEngineConfig(arbitration_enabled=True)
    run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    store.advance_goal(goal_id=hi.id, status="done")
    spawned = run_goal_engine_tick(
        store, convs, now=200, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert spawned == 1  # the former loser now funds
    assert str(store.get_goal(goal_id=lo.id).status) == "assigned"


def test_arbitration_preserves_exactly_once_funding(tmp_path) -> None:
    # Re-ticking the same contended set never double-spawns the winner.
    store, treasury, convs = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=10_000)
    hi = _ready(store, title="hi", expected_value_cents=1000, confidence=1.0)
    _ready(store, title="lo", expected_value_cents=10, confidence=1.0)
    cfg = GoalEngineConfig(arbitration_enabled=True)
    run_goal_engine_tick(
        store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    spawned_again = run_goal_engine_tick(
        store, convs, now=110, treasury=treasury, optimizer=RoiOptimizer(),
        est_cost=100, config=cfg,
    )
    assert spawned_again == 0  # winner already has its live session
    assert sum(1 for c in convs.created if c["external_key"] == f"goal:{hi.id}") == 1
