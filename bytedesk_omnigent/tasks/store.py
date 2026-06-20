"""Durable tasks store: a goal with assignment + execution binding (BDP-2333, ADR-0142).

A ``Task`` is the goal substrate (BDP-2271 C3) plus an explicit **execution
binding** — an ``assignee_agent_id`` (who runs it) distinct from the ``owner_agent_id``
(who is accountable for it) — and a ``required_capability`` that gates which agent may
be assigned. ``claim_task`` is a guarded UPDATE on ``(id, status='open')`` = exactly-once
assignment (ADR-0009 Idempotent Receiver), the same shape ``SqlAlchemyGoalStore`` and the
signal bus / cron scheduler use; ``assign_task`` is the explicit execution-binding write.

``TaskStore`` is the ABC, ``SqlAlchemyTaskStore`` the SqlAlchemy impl, and
``sql_task_to_entity`` the row→entity converter — the store ABC + impl + converter
shape used across the durable substrate. Shares the conversation store's engine like
the goal store, signal bus, and cron scheduler.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from bytedesk_omnigent.db_models import SqlTask
from bytedesk_omnigent.lifecycle import (
    WorkflowLifecycle,
    WorkflowLifecycleStatus,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)

_LIFECYCLE = WorkflowLifecycle()


@dataclass(frozen=True)
class Task:
    """A row of ``tasks`` — a goal with assignment + execution binding."""

    id: str
    title: str
    owner_agent_id: str | None
    assignee_agent_id: str | None
    required_capability: str | None
    status: WorkflowLifecycleStatus
    priority: int
    source: str | None
    payload: dict[str, Any] | None
    created_at: int
    updated_at: int


def sql_task_to_entity(row: SqlTask) -> Task:
    """Convert a ``SqlTask`` row to the immutable :class:`Task` entity.

    Decodes the JSON-in-Text ``payload`` (NULL → ``None``); all other columns map
    one-to-one (mirrors ``goals._to_goal``).
    """
    return Task(
        id=row.id,
        title=row.title,
        owner_agent_id=row.owner_agent_id,
        assignee_agent_id=row.assignee_agent_id,
        required_capability=row.required_capability,
        status=WorkflowLifecycleStatus(row.status),
        priority=row.priority,
        source=row.source,
        payload=json.loads(row.payload) if row.payload is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class TaskStore(ABC):
    """Durable tasks backlog with assignment + execution binding (ADR-0142)."""

    @abstractmethod
    def create_task(
        self,
        *,
        title: str,
        priority: int = 3,
        source: str | None = None,
        required_capability: str | None = None,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> Task:
        """Create an ``open``, unassigned task. Lower ``priority`` sorts first."""

    @abstractmethod
    def list_tasks(
        self,
        *,
        status: str | None = None,
        owner_agent_id: str | None = None,
        assignee_agent_id: str | None = None,
    ) -> list[Task]:
        """List tasks (by priority then age), optionally filtered."""

    @abstractmethod
    def claim_task(
        self, *, task_id: str, owner_agent_id: str, now: int | None = None
    ) -> bool:
        """Atomically assign an ``open`` task to an owner. True iff THIS caller won."""

    @abstractmethod
    def assign_task(
        self,
        *,
        task_id: str,
        assignee_agent_id: str,
        now: int | None = None,
    ) -> bool:
        """Bind an unfinished task to an executing agent. True iff a row was bound."""

    @abstractmethod
    def advance_task(self, *, task_id: str, status: str, now: int | None = None) -> None:
        """Move a task to a new status (``in_progress`` / ``blocked`` / ``done`` …)."""

    @abstractmethod
    def advance_task_owned(
        self,
        *,
        task_id: str,
        status: str,
        owner_agent_id: str,
        now: int | None = None,
    ) -> bool:
        """Advance a task the caller OWNS. True iff the owner's task was advanced."""


class SqlAlchemyTaskStore(TaskStore):
    """Durable tasks store backed by SqlAlchemy (ADR-0142).

    Shares the conversation store's engine (see :func:`get_task_store`), like the
    goal store. ``claim_task`` / ``assign_task`` / ``advance_task_owned`` are guarded
    UPDATEs (ADR-0009), so a missing / foreign / already-claimed task matches 0 rows.
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def create_task(
        self,
        *,
        title: str,
        priority: int = 3,
        source: str | None = None,
        required_capability: str | None = None,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> Task:
        """Create an ``open``, unassigned task. Lower ``priority`` numbers sort first."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlTask(
                id=f"task_{uuid.uuid4().hex}",
                title=title,
                owner_agent_id=None,
                assignee_agent_id=None,
                required_capability=required_capability,
                status="open",
                priority=priority,
                source=source,
                payload=json.dumps(payload) if payload is not None else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return sql_task_to_entity(row)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        owner_agent_id: str | None = None,
        assignee_agent_id: str | None = None,
    ) -> list[Task]:
        """List tasks (by priority then age), optionally filtered."""
        stmt = select(SqlTask)
        if status is not None:
            stmt = stmt.where(SqlTask.status == status)
        if owner_agent_id is not None:
            stmt = stmt.where(SqlTask.owner_agent_id == owner_agent_id)
        if assignee_agent_id is not None:
            stmt = stmt.where(SqlTask.assignee_agent_id == assignee_agent_id)
        stmt = stmt.order_by(SqlTask.priority, SqlTask.created_at)
        with self._session() as session:
            return [sql_task_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def claim_task(
        self, *, task_id: str, owner_agent_id: str, now: int | None = None
    ) -> bool:
        """Atomically assign an ``open`` task. Returns True if THIS caller claimed it.

        Guarded UPDATE on ``(id, status='open')`` — exactly one agent wins (ADR-0009).
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            result = session.execute(
                update(SqlTask)
                .where(SqlTask.id == task_id, SqlTask.status == "open")
                .values(status="assigned", owner_agent_id=owner_agent_id, updated_at=now)
            )
            return result.rowcount == 1

    def assign_task(
        self,
        *,
        task_id: str,
        assignee_agent_id: str,
        now: int | None = None,
    ) -> bool:
        """Bind a not-yet-``done`` task to an executing agent (the execution binding).

        Guarded UPDATE on ``(id, status != 'done')`` so a finished or missing task
        matches 0 rows. Returns True iff this caller set the assignee. Distinct from
        ``owner_agent_id`` (accountability) — a task can be owned by a manager and
        executed by a specialist.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            result = session.execute(
                update(SqlTask)
                .where(SqlTask.id == task_id, SqlTask.status != "done")
                .values(assignee_agent_id=assignee_agent_id, updated_at=now)
            )
            return result.rowcount == 1

    def advance_task(self, *, task_id: str, status: str, now: int | None = None) -> None:
        """Move a task to a new status (``in_progress`` / ``blocked`` / ``done`` …).

        Rejects a genuinely illegal transition (e.g. ``done -> in_progress``) via
        the lifecycle state machine (BDP-2356).
        """
        now = now_epoch() if now is None else now
        target = WorkflowLifecycleStatus(status)
        with self._write_session() as session:
            current = session.get(SqlTask, task_id)
            if current is not None:
                _LIFECYCLE.check(WorkflowLifecycleStatus(current.status), target)
            session.execute(
                update(SqlTask)
                .where(SqlTask.id == task_id)
                .values(status=target, updated_at=now)
            )

    def advance_task_owned(
        self,
        *,
        task_id: str,
        status: str,
        owner_agent_id: str,
        now: int | None = None,
    ) -> bool:
        """Move a task the caller OWNS to a new status (authz, mirrors goals).

        Guarded UPDATE on ``(id, owner_agent_id)`` — an agent can only advance its OWN
        task; a missing / foreign task matches 0 rows. ``open`` returns the task to the
        backlog (clears owner + assignee). Returns True iff this owner's task advanced.
        """
        now = now_epoch() if now is None else now
        target = WorkflowLifecycleStatus(status)
        values: dict[str, Any] = {"status": target, "updated_at": now}
        if target is WorkflowLifecycleStatus.OPEN:
            values["owner_agent_id"] = None
            values["assignee_agent_id"] = None
        with self._write_session() as session:
            current = session.get(SqlTask, task_id)
            if current is not None and current.owner_agent_id == owner_agent_id:
                _LIFECYCLE.check(WorkflowLifecycleStatus(current.status), target)
            result = session.execute(
                update(SqlTask)
                .where(
                    SqlTask.id == task_id,
                    SqlTask.owner_agent_id == owner_agent_id,
                )
                .values(**values)
            )
            return result.rowcount == 1


_task_store_cache: dict[str, SqlAlchemyTaskStore] = {}


def get_task_store() -> SqlAlchemyTaskStore:
    """Return the durable tasks store (BDP-2333, ADR-0142).

    Shares the conversation store's engine + storage location, cached per location
    (mirrors :func:`bytedesk_omnigent.goals.get_goal_store`).
    """
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _task_store_cache.get(location)
    if store is None:
        store = SqlAlchemyTaskStore(location)
        _task_store_cache[location] = store
    return store
