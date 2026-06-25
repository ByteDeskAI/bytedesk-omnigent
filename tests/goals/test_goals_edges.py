"""Edge tests for goals store lifecycle, escalation, and cache."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from bytedesk_omnigent.goals import SqlAlchemyGoalStore, get_goal_store
from bytedesk_omnigent.lifecycle import IllegalTransition


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_engine_property_and_owner_filter(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="mine", priority=1, now=now)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now)
    store.create_goal(title="theirs", priority=2, now=now)

    assert store.engine is not None
    assert [g.title for g in store.list_goals(owner_agent_id="maya")] == ["mine"]


def test_advance_goal_blocked_resets_escalated_at(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="stuck", now=now)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now)
    store.advance_goal(goal_id=goal.id, status="blocked", now=now + 1)

    escalated = store.escalate_blocked(now=now + 2)
    assert [g.id for g in escalated] == [goal.id]
    assert store.escalate_blocked(now=now + 3) == []

    store.advance_goal(goal_id=goal.id, status="in_progress", now=now + 4)
    store.advance_goal(goal_id=goal.id, status="blocked", now=now + 5)
    assert len(store.escalate_blocked(now=now + 6)) == 1


def test_advance_goal_owned_success_and_failure(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="owned", now=now)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now)

    assert (
        store.advance_goal_owned(
            goal_id=goal.id, status="in_progress", owner_agent_id="maya", now=now + 1
        )
        is True
    )
    assert (
        store.advance_goal_owned(
            goal_id=goal.id, status="blocked", owner_agent_id="maya", now=now + 2
        )
        is True
    )
    assert store.list_goals(status="blocked")[0].owner_agent_id == "maya"

    assert (
        store.advance_goal_owned(
            goal_id=goal.id, status="open", owner_agent_id="maya", now=now + 3
        )
        is True
    )
    reopened = store.list_goals(status="open")[0]
    assert reopened.owner_agent_id is None

    other = store.create_goal(title="foreign", now=now + 3)
    store.claim_goal(goal_id=other.id, owner_agent_id="caleb", now=now + 3)
    assert (
        store.advance_goal_owned(
            goal_id=other.id, status="done", owner_agent_id="maya", now=now + 4
        )
        is False
    )


def test_reopen_stalled_returns_prior_owner(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="stale", now=now - 1000)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now - 1000)
    store.advance_goal(goal_id=goal.id, status="assigned", now=now - 1000)

    reopened = store.reopen_stalled(older_than_seconds=60, now=now)
    assert len(reopened) == 1
    assert reopened[0].owner_agent_id == "maya"
    assert store.list_goals(status="open")[0].owner_agent_id is None


def test_advance_goal_rejects_illegal_transition(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="done", now=now)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now)
    store.advance_goal(goal_id=goal.id, status="done", now=now + 1)

    with pytest.raises(IllegalTransition):
        store.advance_goal(goal_id=goal.id, status="in_progress", now=now + 2)


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_goal_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_goal_store.__globals__["_goal_store_cache"].clear()

    first = get_goal_store()
    second = get_goal_store()
    assert first is second
    assert isinstance(first, SqlAlchemyGoalStore)
