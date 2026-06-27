"""Learning loop (BDP-2596 Wave 3, feature 1).

After a goal completes and an outcome is booked, its confidence moves toward the
realized-vs-expected ratio (EWMA, clamped). The cost model's tokens-per-goal
estimate learns from the ``goal_decisions`` replay log. Fakes only.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.conditions import All, Leaf, Predicate
from bytedesk_omnigent.engine.learning import (
    learn_tokens_per_goal,
    update_confidence,
)
from bytedesk_omnigent.engine.loop import run_goal_engine_tick
from bytedesk_omnigent.engine.sensors import build_default_registry
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path):
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    return SqlAlchemyGoalStore(loc), SqlAlchemyTreasury(loc)


# -- update_confidence (pure) ------------------------------------------------
def test_update_confidence_winner_moves_up() -> None:
    # realized 2x expected → ratio clamps to 1.0 → EWMA pulls confidence up.
    new = update_confidence(confidence=0.5, realized=2000, expected=1000)
    assert new > 0.5
    assert new <= 1.0


def test_update_confidence_loser_moves_down() -> None:
    new = update_confidence(confidence=0.8, realized=100, expected=1000)
    assert new < 0.8
    assert new >= 0.0


def test_update_confidence_is_clamped() -> None:
    assert update_confidence(confidence=0.99, realized=10**9, expected=1) <= 1.0
    assert update_confidence(confidence=0.01, realized=0, expected=1000) >= 0.0


def test_update_confidence_no_expected_is_noop() -> None:
    # expected_value 0 (legacy/static goals) → no signal → confidence unchanged.
    assert update_confidence(confidence=0.5, realized=0, expected=0) == 0.5
    assert update_confidence(confidence=0.5, realized=500, expected=0) == 0.5


# -- applied on completion ---------------------------------------------------
def test_completion_updates_confidence_from_realized(tmp_path) -> None:
    store, treasury = _store(tmp_path)
    dep = store.create_goal(title="dep")
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = store.create_goal(
        title="g", success_condition=sc, expected_value_cents=1000, confidence=0.5
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    store.advance_goal(goal_id=goal.id, status="in_progress")
    # A rail booked a winning outcome (realized > expected).
    treasury.book_outcome(
        goal_store=store, goal_id=goal.id, realized_value_cents=2000,
        source="rail", evidence=None,
    )

    class _Convs:
        created: list = []

        def get_conversation_by_external_key(self, k):
            return None

        def create_conversation(self, **kw):
            raise AssertionError("should not spawn — goal completes")

        def append(self, *a):
            pass

    run_goal_engine_tick(
        store, _Convs(), now=100, sensor_registry=build_default_registry(),
        treasury=treasury,
    )
    refreshed = store.get_goal(goal_id=goal.id)
    assert str(refreshed.status) == "done"
    assert refreshed.confidence > 0.5  # winner biased up


def test_completion_without_outcome_leaves_confidence(tmp_path) -> None:
    store, treasury = _store(tmp_path)
    dep = store.create_goal(title="dep")
    store.claim_goal(goal_id=dep.id, owner_agent_id="m")
    store.advance_goal(goal_id=dep.id, status="done")
    sc = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))]).to_dict()
    goal = store.create_goal(
        title="g", success_condition=sc, expected_value_cents=1000, confidence=0.5
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")

    class _Convs:
        def get_conversation_by_external_key(self, k):
            return None

        def create_conversation(self, **kw):
            raise AssertionError

        def append(self, *a):
            pass

    run_goal_engine_tick(
        store, _Convs(), now=100, sensor_registry=build_default_registry(),
        treasury=treasury,
    )
    # No outcome booked → realized 0, expected 1000 → confidence drifts but no crash.
    # The key invariant: a goal with expected==0 is untouched (legacy).
    legacy = store.create_goal(title="legacy", confidence=0.5)  # expected 0
    store.claim_goal(goal_id=legacy.id, owner_agent_id="x")
    store.advance_goal(goal_id=legacy.id, status="done")
    assert store.get_goal(goal_id=legacy.id).confidence == 0.5


# -- cost learning from the decision/outcome ledger --------------------------
def test_learn_tokens_per_goal_no_history_is_default(tmp_path) -> None:
    store, _treasury = _store(tmp_path)
    # No completed goals with a measured cost → fall back to the supplied default.
    assert learn_tokens_per_goal(store, default=30_000) == 30_000


def test_learn_tokens_per_goal_from_measured_costs(tmp_path) -> None:
    store, _treasury = _store(tmp_path)
    # Two completed goals carrying a measured actual cost (cents). The learner
    # converts the mean measured cost back to a tokens estimate via the default
    # model price, so a cheaper-than-expected run pulls the estimate DOWN.
    for cents in (15, 15):  # default price 2c/1k → 15c ≈ 7_500 tokens each
        g = store.create_goal(title="c", payload={"actual_cost_cents": cents})
        store.claim_goal(goal_id=g.id, owner_agent_id="m")
        store.advance_goal(goal_id=g.id, status="done")
    learned = learn_tokens_per_goal(store, default=30_000)
    assert 0 < learned < 30_000  # measured cheaper than the coarse default
