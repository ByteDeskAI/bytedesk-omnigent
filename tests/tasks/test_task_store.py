"""Tests for the durable tasks store — a goal with assignment + execution binding
(BDP-2333, ADR-0142)."""

from __future__ import annotations

import time

from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore


def _store(tmp_path) -> SqlAlchemyTaskStore:
    return SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")


def test_create_list_and_claim_task_exactly_once(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    high = store.create_task(
        title="ship release",
        priority=1,
        source="cron",
        required_capability="release.execute",
        payload={"branch": "release/0.3.0"},
        now=now,
    )
    store.create_task(title="write docs", priority=5, source="cron", now=now)

    # Open tasks list by priority (lower first); the envelope round-trips.
    open_tasks = store.list_tasks(status="open")
    assert [t.title for t in open_tasks] == ["ship release", "write docs"]
    assert open_tasks[0].required_capability == "release.execute"
    assert open_tasks[0].payload == {"branch": "release/0.3.0"}
    assert open_tasks[0].assignee_agent_id is None
    assert store.get_task(high.id) == high
    assert store.get_task("task_missing") is None

    # First claim of an open task wins; a second claim loses (no longer open).
    assert store.claim_task(task_id=high.id, owner_agent_id="maya", now=now) is True
    assert store.claim_task(task_id=high.id, owner_agent_id="caleb", now=now) is False

    assigned = store.list_tasks(status="assigned")
    assert [t.id for t in assigned] == [high.id]
    assert assigned[0].owner_agent_id == "maya"

    store.advance_task(task_id=high.id, status="done", now=now + 1)
    assert store.list_tasks(status="done")[0].id == high.id


def test_assign_task_binds_executor_distinct_from_owner(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    task = store.create_task(title="build feature", priority=2, now=now)
    store.claim_task(task_id=task.id, owner_agent_id="maya", now=now)

    # The execution binding is distinct from accountability ownership.
    assert store.assign_task(task_id=task.id, assignee_agent_id="elias", now=now) is True
    bound = store.list_tasks(assignee_agent_id="elias")
    assert [t.id for t in bound] == [task.id]
    assert bound[0].owner_agent_id == "maya"
    assert bound[0].assignee_agent_id == "elias"

    # A done task can no longer be re-bound (guarded UPDATE matches 0 rows).
    store.advance_task(task_id=task.id, status="done", now=now + 1)
    assert store.assign_task(task_id=task.id, assignee_agent_id="priya", now=now + 1) is False

    # A missing task never binds.
    assert store.assign_task(task_id="task_missing", assignee_agent_id="elias", now=now) is False


def test_advance_task_owned_authz_and_reopen(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    task = store.create_task(title="triage", priority=3, now=now)
    store.claim_task(task_id=task.id, owner_agent_id="maya", now=now)
    store.assign_task(task_id=task.id, assignee_agent_id="elias", now=now)

    # A foreign agent cannot advance someone else's task.
    assert (
        store.advance_task_owned(
            task_id=task.id, status="in_progress", owner_agent_id="caleb", now=now
        )
        is False
    )
    # The owner can.
    assert (
        store.advance_task_owned(
            task_id=task.id, status="in_progress", owner_agent_id="maya", now=now
        )
        is True
    )
    # Reopening returns it to the backlog: clears owner + assignee.
    assert (
        store.advance_task_owned(
            task_id=task.id, status="open", owner_agent_id="maya", now=now + 1
        )
        is True
    )
    reopened = store.list_tasks(status="open")
    assert [t.id for t in reopened] == [task.id]
    assert reopened[0].owner_agent_id is None
    assert reopened[0].assignee_agent_id is None
