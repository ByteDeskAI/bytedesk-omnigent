"""Tests for the durable goals backlog + ops scoreboard (BDP-2271 C3, ADR-0142)."""
from __future__ import annotations

import time

from omnigent.goals import SqlAlchemyGoalStore


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


def test_scoreboard_upsert_and_ranking(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    store.record_score(agent_id="maya", metric="tasks_completed", value=3, now=now)
    store.record_score(agent_id="caleb", metric="tasks_completed", value=7, now=now)
    # Upsert: re-recording overwrites, not duplicates.
    store.record_score(agent_id="maya", metric="tasks_completed", value=9, now=now + 1)

    ranked = store.scoreboard(metric="tasks_completed")
    assert ranked == [("maya", 9.0), ("caleb", 7.0)]
