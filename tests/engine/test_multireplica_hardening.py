"""Multi-replica / multi-tenant correctness (BDP-2589, Phase 7).

Two concurrent ticks against the SAME store must dispatch a ready goal exactly
once (the dispatcher's unique ``external_key`` guarantees it) and reserve budget
exactly once (the guarded conditional UPDATE guarantees it). This pins those
exactly-once invariants — the cross-replica single-writer is the PG advisory lock
in ``goal_engine_loop`` (a no-op on SQLite, same-DB), and these constraints back
it up even if two ticks raced.
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
    """A shared store both 'replicas' dispatch against (one row per external_key)."""

    def __init__(self) -> None:
        self.by_external_key: dict[str, _FakeConversation] = {}
        self.created: list[dict] = []
        self._n = 0

    def get_conversation_by_external_key(self, external_key):
        return self.by_external_key.get(external_key)

    def create_conversation(self, **kwargs):
        external_key = kwargs.get("external_key")
        # Mimic the UNIQUE(external_key) constraint: a second insert is a no-op
        # that returns the existing row (idempotent dispatch).
        if external_key is not None and external_key in self.by_external_key:
            return self.by_external_key[external_key]
        self._n += 1
        conv = _FakeConversation(id=f"conv_{self._n}", external_key=external_key)
        self.created.append(kwargs)
        if external_key is not None:
            self.by_external_key[external_key] = conv
        return conv

    def append(self, conversation_id, items):
        pass


def _setup(tmp_path):
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    return SqlAlchemyGoalStore(loc), SqlAlchemyTreasury(loc), _FakeConversationStore()


def _ready(store, **kw):
    goal = store.create_goal(**kw)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    return goal


def test_two_ticks_dispatch_once(tmp_path) -> None:
    store, _t, convs = _setup(tmp_path)
    _ready(store, title="g")
    # Two ticks (two replicas) against the same store; idempotent external_key.
    first = run_goal_engine_tick(store, convs, now=100)
    second = run_goal_engine_tick(store, convs, now=101)
    assert first == 1
    assert second == 0  # re-tick is a no-op once the session exists
    assert len(convs.created) == 1


def test_reserve_is_exactly_once_under_replay(tmp_path) -> None:
    store, treasury, _c = _setup(tmp_path)
    treasury.set_budget(tier="org", target_id="omnigent", cap_cents=1000)
    goal = _ready(store, title="g", expected_value_cents=500)
    r1 = treasury.reserve(goal, 100, now=100)
    r2 = treasury.reserve(goal, 100, now=101)  # same period → idempotent no-op
    assert r1 is not None
    assert r2 is None
    assert treasury.spent_cents(tier="org", target_id="omnigent") == 100  # charged once


def test_book_outcome_is_exactly_once_under_replay(tmp_path) -> None:
    store, treasury, _c = _setup(tmp_path)
    goal = _ready(store, title="g")
    treasury.book_outcome(
        goal_store=store, goal_id=goal.id, realized_value_cents=400,
        source="test", evidence=None, now=100,
    )
    after = store.get_goal(goal_id=goal.id)
    assert after.realized_value_cents == 400  # single guarded increment


def test_portfolio_tick_funds_each_goal_once(tmp_path) -> None:
    store, treasury, convs = _setup(tmp_path)
    g = _ready(store, title="g", expected_value_cents=1000)
    a = run_goal_engine_tick(store, convs, now=100, treasury=treasury, optimizer=RoiOptimizer())
    b = run_goal_engine_tick(store, convs, now=101, treasury=treasury, optimizer=RoiOptimizer())
    assert a == 1
    assert b == 0
    assert len(convs.created) == 1
    # exactly one 'funded' decision for the goal.
    funded = [d for d in treasury.decisions(goal_id=g.id) if d.reason == "funded"]
    assert len(funded) == 1
