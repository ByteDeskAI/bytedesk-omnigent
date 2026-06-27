"""Tests for the Goal Dispatcher — goal-ready → an agent works it (BDP-2583)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from bytedesk_omnigent.engine.dispatcher import dispatch_goal
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


@dataclass
class _FakeConversation:
    id: str
    external_key: str | None


class _FakeConversationStore:
    """In-memory stand-in for the bits dispatch_goal touches (DispatchProtocol)."""

    def __init__(self) -> None:
        self.by_external_key: dict[str, _FakeConversation] = {}
        self.created: list[dict] = []
        self.appended: list[tuple[str, str]] = []
        self._n = 0

    def get_conversation_by_external_key(self, external_key: str) -> _FakeConversation | None:
        return self.by_external_key.get(external_key)

    def create_conversation(self, **kwargs) -> _FakeConversation:
        self._n += 1
        conv = _FakeConversation(id=f"conv_{self._n}", external_key=kwargs.get("external_key"))
        self.created.append(kwargs)
        if conv.external_key is not None:
            self.by_external_key[conv.external_key] = conv
        return conv

    def append(self, conversation_id: str, items) -> None:
        for item in items:
            text = item.data.content[0]["text"]
            self.appended.append((conversation_id, text))


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_dispatch_spawns_session_for_ready_goal(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="Ship the release", source="cron")
    store.update_goal(goal_id=goal.id, payload={"x": 1})  # owner still None
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    goal = store.get_goal(goal_id=goal.id)

    result = dispatch_goal(goal, conversation_store=convs, goal_store=store, now=int(time.time()))

    assert result.spawned is True
    assert result.session_id == "conv_1"
    assert convs.created[0]["agent_id"] == "maya"
    # the goal's intent reaches the new session as the opening message
    assert any("Ship the release" in text for _, text in convs.appended)


def test_dispatch_is_idempotent_within_a_period(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="Daily standup", source="cron")
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    goal = store.get_goal(goal_id=goal.id)

    first = dispatch_goal(goal, conversation_store=convs, goal_store=store, now=100)
    second = dispatch_goal(goal, conversation_store=convs, goal_store=store, now=200)

    assert first.spawned is True
    assert second.spawned is False
    assert second.session_id == first.session_id
    assert len(convs.created) == 1  # one live session per (goal, period)


def test_dispatch_recurring_uses_period_key_per_fire(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="Hourly sweep", source="cron")
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya")
    goal = store.get_goal(goal_id=goal.id)

    a = dispatch_goal(
        goal, conversation_store=convs, goal_store=store, now=100, period_key=f"{goal.id}:1000"
    )
    a2 = dispatch_goal(
        goal, conversation_store=convs, goal_store=store, now=110, period_key=f"{goal.id}:1000"
    )
    b = dispatch_goal(
        goal, conversation_store=convs, goal_store=store, now=200, period_key=f"{goal.id}:2000"
    )

    assert a.spawned is True
    assert a2.spawned is False  # same fire period → no-op
    assert b.spawned is True  # next fire period → a fresh session
    assert len({c.session_id for c in (a, b)}) == 2


def test_dispatch_unowned_goal_does_not_spawn(tmp_path) -> None:
    store = _store(tmp_path)
    convs = _FakeConversationStore()
    goal = store.create_goal(title="Needs an owner", source="cron")  # owner_agent_id is None

    result = dispatch_goal(goal, conversation_store=convs, goal_store=store, now=100)

    assert result.spawned is False
    assert result.session_id is None
    assert convs.created == []
