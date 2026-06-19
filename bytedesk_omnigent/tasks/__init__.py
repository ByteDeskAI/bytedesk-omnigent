"""First-class tasks: a goal with assignment + execution binding (BDP-2333, ADR-0142).

A ``Task`` extends the goal shape (durable backlog row, guarded-UPDATE claim) with
an explicit **assignee** (who executes it) distinct from its **owner** (who is
accountable for it) and a **capability requirement** that gates which agent may be
assigned. The :class:`~bytedesk_omnigent.tasks.store.SqlAlchemyTaskStore` is the
durable store; :func:`~bytedesk_omnigent.tasks.router.create_tasks_router` exposes
the read API; both mirror the goals substrate (BDP-2271 C3).
"""

from __future__ import annotations

from bytedesk_omnigent.tasks.store import (
    SqlAlchemyTaskStore,
    Task,
    TaskStore,
    get_task_store,
    sql_task_to_entity,
)

__all__ = [
    "SqlAlchemyTaskStore",
    "Task",
    "TaskStore",
    "get_task_store",
    "sql_task_to_entity",
]
