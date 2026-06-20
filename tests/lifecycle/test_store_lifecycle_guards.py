"""Store-level lifecycle adoption tests (BDP-2356, ADR-0142).

Proves the durable stores (1) coerce the raw DB status string into the shared
StrEnum on read, (2) accept the SAME legal transition sequences that worked
before, and (3) now reject a genuinely-illegal transition via the
:class:`LifecycleStateMachine` guard.
"""

from __future__ import annotations

import time

import pytest

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.lifecycle import IllegalTransition, WorkflowLifecycleStatus
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore


def _task_store(tmp_path) -> SqlAlchemyTaskStore:
    return SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")


def _goal_store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def test_task_status_is_coerced_to_shared_enum(tmp_path) -> None:
    store = _task_store(tmp_path)
    task = store.create_task(title="t", now=int(time.time()))
    assert task.status is WorkflowLifecycleStatus.OPEN
    assert task.status == "open"  # wire-compat: equals the legacy string


def test_goal_status_is_coerced_to_shared_enum(tmp_path) -> None:
    store = _goal_store(tmp_path)
    goal = store.create_goal(title="g", now=int(time.time()))
    assert goal.status is WorkflowLifecycleStatus.OPEN
    assert goal.status == "open"


def test_task_and_goal_share_one_status_enum(tmp_path) -> None:
    """Both substrates surface the same WorkflowLifecycleStatus member."""
    now = int(time.time())
    task = _task_store(tmp_path).create_task(title="t", now=now)
    goal = _goal_store(tmp_path).create_goal(title="g", now=now)
    assert type(task.status) is type(goal.status) is WorkflowLifecycleStatus


def test_task_legal_transition_sequence_still_passes(tmp_path) -> None:
    store = _task_store(tmp_path)
    now = int(time.time())
    task = store.create_task(title="t", now=now)
    store.claim_task(task_id=task.id, owner_agent_id="maya", now=now)  # open->assigned
    store.advance_task(task_id=task.id, status="in_progress", now=now)
    store.advance_task(task_id=task.id, status="blocked", now=now)
    store.advance_task(task_id=task.id, status="in_progress", now=now)
    store.advance_task(task_id=task.id, status="done", now=now)
    assert store.list_tasks(status="done")[0].id == task.id


def test_task_advance_rejects_illegal_transition_from_terminal(tmp_path) -> None:
    store = _task_store(tmp_path)
    now = int(time.time())
    task = store.create_task(title="t", now=now)
    store.advance_task(task_id=task.id, status="done", now=now)  # open->done legal
    with pytest.raises(IllegalTransition):
        store.advance_task(task_id=task.id, status="in_progress", now=now)


def test_task_advance_owned_rejects_illegal_transition(tmp_path) -> None:
    store = _task_store(tmp_path)
    now = int(time.time())
    task = store.create_task(title="t", now=now)
    store.claim_task(task_id=task.id, owner_agent_id="maya", now=now)
    assert store.advance_task_owned(task_id=task.id, status="done", owner_agent_id="maya", now=now)
    with pytest.raises(IllegalTransition):
        store.advance_task_owned(task_id=task.id, status="open", owner_agent_id="maya", now=now)


def test_goal_advance_rejects_illegal_transition_from_terminal(tmp_path) -> None:
    store = _goal_store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="g", now=now)
    store.advance_goal(goal_id=goal.id, status="done", now=now)
    with pytest.raises(IllegalTransition):
        store.advance_goal(goal_id=goal.id, status="in_progress", now=now)


def test_goal_legal_blocked_unblock_sequence_still_passes(tmp_path) -> None:
    store = _goal_store(tmp_path)
    now = int(time.time())
    goal = store.create_goal(title="g", now=now)
    store.claim_goal(goal_id=goal.id, owner_agent_id="maya", now=now)
    store.advance_goal(goal_id=goal.id, status="blocked", now=now)
    store.advance_goal(goal_id=goal.id, status="in_progress", now=now)
    store.advance_goal(goal_id=goal.id, status="done", now=now)
    assert store.list_goals(status="done")[0].id == goal.id
