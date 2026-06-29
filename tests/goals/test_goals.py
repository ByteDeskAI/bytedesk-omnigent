"""Tests for the durable goals backlog + ops scoreboard (BDP-2271 C3, ADR-0142)."""
from __future__ import annotations

import time

from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_create_list_and_claim_goal_exactly_once(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    high = store.create_goal(title="ship release", priority=1, source="cron", now=now)
    store.create_goal(title="write docs", priority=5, source="cron", now=now)

    # Open goals list by priority (lower first).
    open_titles = [g.title for g in store.list_goals(status="open")]
    assert open_titles == ["ship release", "write docs"]

    # First claim of an open goal wins; a second claim loses (no longer open).
    assert store.claim_goal(goal_id=high.id, owner_agent_id="maya", now=now) is True
    assert store.claim_goal(goal_id=high.id, owner_agent_id="caleb", now=now) is False

    assigned = store.list_goals(status="assigned")
    assert [g.id for g in assigned] == [high.id]
    assert assigned[0].owner_agent_id == "maya"

    store.advance_goal(goal_id=high.id, status="done", now=now + 1)
    assert store.list_goals(status="done")[0].id == high.id


def test_dependent_goal_waits_until_dependencies_resolve(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="Launch reporting",
        target_kind="department",
        target_id="Operations",
        target_label="Operations",
        dependencies=[{"kind": "system_state", "label": "warehouse export ready"}],
        now=100,
    )

    assert goal.readiness_kind == "dependent"
    assert goal.activation_state == "waiting"
    assert store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=101) is False

    dependency = goal.dependencies[0]
    store.update_dependency(
        goal_id=goal.id,
        dependency_id=dependency.id,
        status="satisfied",
        now=102,
    )
    ready = store.get_goal(goal_id=goal.id)

    assert ready is not None
    assert ready.activation_state == "ready"
    assert ready.dependencies[0].status == "satisfied"
    assert store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=103) is True


def test_goal_taxonomy_fields_round_trip_and_filter(tmp_path) -> None:
    store = _store(tmp_path)

    roadmap = store.create_goal(
        title="Ship Office roadmap item",
        target_kind="department",
        target_id="development",
        department_slug="development",
        outcome_kind="roadmap",
        now=100,
    )
    store.create_goal(
        title="Book new revenue",
        target_kind="organization",
        outcome_kind="financial",
        now=100,
    )

    stored = store.get_goal(goal_id=roadmap.id)
    assert stored is not None
    assert stored.department_slug == "development"
    assert stored.outcome_kind == "roadmap"

    by_department = store.list_goals(department_slug="development")
    assert [g.id for g in by_department] == [roadmap.id]

    by_kind = store.list_goals(outcome_kind="financial")
    assert [g.title for g in by_kind] == ["Book new revenue"]


def test_outcome_correlation_resolves_goal_from_external_subject(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="Close opportunity", now=100)

    store.record_goal_correlation(
        source="sales",
        subject_ref="opp-123",
        goal_id=goal.id,
        kind="opportunity",
        tenant_id="tenant-1",
        now=101,
    )

    assert store.resolve_goal_correlation(source="sales", subject_ref="opp-123") == goal.id
    assert store.resolve_goal_correlation(source="sales", subject_ref="missing") is None


def test_scoreboard_upsert_and_ranking(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    store.record_score(agent_id="maya", metric="tasks_completed", value=3, now=now)
    store.record_score(agent_id="caleb", metric="tasks_completed", value=7, now=now)
    # Upsert: re-recording overwrites, not duplicates.
    store.record_score(agent_id="maya", metric="tasks_completed", value=9, now=now + 1)

    ranked = store.scoreboard(metric="tasks_completed")
    assert ranked == [("maya", 9.0), ("caleb", 7.0)]
