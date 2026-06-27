"""Tests for the goal resolver — condition tree → actionable + waiting_reasons (BDP-2584)."""
from __future__ import annotations

from bytedesk_omnigent.engine.conditions import All, Any, Leaf, Predicate
from bytedesk_omnigent.engine.resolver import CONDITION_PAYLOAD_KEY, resolve
from bytedesk_omnigent.engine.sensors import build_default_registry
from bytedesk_omnigent.goals import SqlAlchemyGoalStore, _activation_for


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _reg():
    return build_default_registry()


# -- AST-driven resolution ---------------------------------------------------
def test_actionable_when_condition_met(tmp_path) -> None:
    store = _store(tmp_path)
    dep = store.create_goal(title="upstream", now=10)
    store.claim_goal(goal_id=dep.id, owner_agent_id="m", now=11)
    store.advance_goal(goal_id=dep.id, status="in_progress", now=12)
    store.advance_goal(goal_id=dep.id, status="done", now=13)

    tree = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))])
    goal = store.create_goal(
        title="downstream", payload={CONDITION_PAYLOAD_KEY: tree.to_dict()}, now=20
    )

    result = resolve(goal, registry=_reg(), goal_store=store, now=100)
    assert result["actionable"] is True
    assert result["waiting_reasons"] == []
    assert result["freshness_s"] == 60  # goal_outcome stale_after_s


def test_waiting_reasons_when_unmet(tmp_path) -> None:
    store = _store(tmp_path)
    dep = store.create_goal(title="upstream", now=10)  # still open
    tree = All([Leaf("goal_outcome", {"goal_id": dep.id}, Predicate("equals", "done"))])
    goal = store.create_goal(
        title="downstream", payload={CONDITION_PAYLOAD_KEY: tree.to_dict()}, now=20
    )

    result = resolve(goal, registry=_reg(), goal_store=store, now=100)
    assert result["actionable"] is False
    assert len(result["waiting_reasons"]) == 1
    assert "goal_outcome" in result["waiting_reasons"][0]
    assert dep.id in result["waiting_reasons"][0]


def test_any_branch_actionable_with_one_met(tmp_path) -> None:
    store = _store(tmp_path)
    a = store.create_goal(title="a", now=10)
    store.claim_goal(goal_id=a.id, owner_agent_id="m", now=11)
    store.advance_goal(goal_id=a.id, status="in_progress", now=12)
    store.advance_goal(goal_id=a.id, status="done", now=13)
    b = store.create_goal(title="b", now=10)  # open

    tree = Any(
        [
            Leaf("goal_outcome", {"goal_id": a.id}, Predicate("exists")),
            Leaf("goal_outcome", {"goal_id": b.id}, Predicate("exists")),
        ]
    )
    goal = store.create_goal(title="g", payload={CONDITION_PAYLOAD_KEY: tree.to_dict()}, now=20)
    assert resolve(goal, registry=_reg(), goal_store=store, now=100)["actionable"] is True


# -- backward-compat: legacy dependency goals resolve like _activation_for ---
def test_legacy_dependent_goal_waiting_matches_activation_for(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="legacy",
        dependencies=[{"kind": "system_state", "label": "export ready"}],
        now=10,
    )
    # _activation_for says "waiting" while the dep is pending.
    assert _activation_for(goal.readiness_kind, [d.status for d in goal.dependencies]) == "waiting"
    result = resolve(goal, registry=_reg(), goal_store=store, now=100)
    assert result["actionable"] is False
    assert result["waiting_reasons"]


def test_legacy_dependent_goal_ready_when_deps_satisfied(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="legacy",
        dependencies=[{"kind": "system_state", "label": "export ready"}],
        now=10,
    )
    store.update_dependency(
        goal_id=goal.id, dependency_id=goal.dependencies[0].id, status="satisfied", now=11
    )
    goal = store.get_goal(goal_id=goal.id)
    assert _activation_for(goal.readiness_kind, [d.status for d in goal.dependencies]) == "ready"
    assert resolve(goal, registry=_reg(), goal_store=store, now=100)["actionable"] is True


def test_legacy_immediate_goal_is_actionable(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="immediate", now=10)  # no deps, readiness=immediate
    result = resolve(goal, registry=_reg(), goal_store=store, now=100)
    assert result["actionable"] is True
    assert result["waiting_reasons"] == []
    assert result["freshness_s"] is None  # no readings → no freshness


def test_legacy_deferred_goal_is_not_actionable(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="deferred", readiness_kind="deferred", now=10)
    result = resolve(goal, registry=_reg(), goal_store=store, now=100)
    assert result["actionable"] is False
    assert result["waiting_reasons"]


# -- Phase 1 tick wiring (additive) ------------------------------------------
def test_tick_holds_back_goal_with_unmet_condition(tmp_path) -> None:
    from bytedesk_omnigent.engine.loop import run_goal_engine_tick
    from tests.engine.test_dispatcher import _FakeConversationStore

    store = _store(tmp_path)
    upstream = store.create_goal(title="upstream", now=10)  # open, not done
    tree = All([Leaf("goal_outcome", {"goal_id": upstream.id}, Predicate("equals", "done"))])
    goal = store.create_goal(
        title="downstream", payload={CONDITION_PAYLOAD_KEY: tree.to_dict()}, now=20
    )
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=21)

    convs = _FakeConversationStore()
    # With a registry the unmet AST holds it back; without one Phase 1 dispatches it.
    held = run_goal_engine_tick(store, convs, now=100, sensor_registry=_reg())
    assert held == 0
    assert convs.created == []

    legacy = run_goal_engine_tick(store, convs, now=100)
    assert legacy == 1  # Phase 1 behaviour unchanged when no registry passed
