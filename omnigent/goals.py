"""Durable goals backlog + ops scoreboard (BDP-2271 C3, ADR-0142).

The "why-act" substrate: a clock without a backlog wakes agents to an empty
desk. ``SqlAlchemyGoalStore`` is the durable backlog a cron-woken triage agent
pulls from (``claim_goal`` is a guarded UPDATE = exactly-once assignment, ADR-0009)
plus the ops scoreboard that workload-rebalance / find-specialist read. Shares
the conversation store's engine, like the signal bus + cron scheduler. The
``bytedesk_goal_*`` / ``bytedesk_ops_scoreboard_*`` agent tools wrap this store
(integration follow-up).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update

from omnigent.db.db_models import SqlGoal, SqlScoreboardEntry
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class Goal:
    """A row of ``goals``."""

    id: str
    title: str
    owner_agent_id: str | None
    status: str
    priority: int
    source: str | None
    payload: dict | None
    created_at: int


def _to_goal(row: SqlGoal) -> Goal:
    return Goal(
        id=row.id,
        title=row.title,
        owner_agent_id=row.owner_agent_id,
        status=row.status,
        priority=row.priority,
        source=row.source,
        payload=json.loads(row.payload) if row.payload is not None else None,
        created_at=row.created_at,
    )


class SqlAlchemyGoalStore:
    """Durable goals backlog + ops scoreboard (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    # ── goals ────────────────────────────────────────────────────────
    def create_goal(
        self,
        *,
        title: str,
        priority: int = 3,
        source: str | None = None,
        payload: dict | None = None,
        now: int | None = None,
    ) -> Goal:
        """Create an ``open`` goal. Lower ``priority`` numbers sort first."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlGoal(
                id=f"goal_{uuid.uuid4().hex}",
                title=title,
                owner_agent_id=None,
                status="open",
                priority=priority,
                source=source,
                payload=json.dumps(payload) if payload is not None else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return _to_goal(row)

    def list_goals(
        self, *, status: str | None = None, owner_agent_id: str | None = None
    ) -> list[Goal]:
        """List goals (by priority then age), optionally filtered."""
        stmt = select(SqlGoal)
        if status is not None:
            stmt = stmt.where(SqlGoal.status == status)
        if owner_agent_id is not None:
            stmt = stmt.where(SqlGoal.owner_agent_id == owner_agent_id)
        stmt = stmt.order_by(SqlGoal.priority, SqlGoal.created_at)
        with self._session() as session:
            return [_to_goal(r) for r in session.execute(stmt).scalars().all()]

    def claim_goal(self, *, goal_id: str, owner_agent_id: str, now: int | None = None) -> bool:
        """Atomically assign an ``open`` goal. Returns True if THIS caller claimed it.

        Guarded UPDATE on ``(id, status='open')`` — exactly one agent wins (ADR-0009).
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            result = session.execute(
                update(SqlGoal)
                .where(SqlGoal.id == goal_id, SqlGoal.status == "open")
                .values(status="assigned", owner_agent_id=owner_agent_id, updated_at=now)
            )
            return result.rowcount == 1

    def advance_goal(self, *, goal_id: str, status: str, now: int | None = None) -> None:
        """Move a goal to a new status (``in_progress`` / ``blocked`` / ``done`` …)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            session.execute(
                update(SqlGoal)
                .where(SqlGoal.id == goal_id)
                .values(status=status, updated_at=now)
            )

    # ── scoreboard ───────────────────────────────────────────────────
    def record_score(
        self,
        *,
        agent_id: str,
        metric: str,
        value: float,
        window: str = "all",
        now: int | None = None,
    ) -> None:
        """Upsert the latest value for ``(agent_id, metric, window)``."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            existing = session.get(SqlScoreboardEntry, (agent_id, metric, window))
            if existing is not None:
                existing.value = value
                existing.updated_at = now
            else:
                session.add(
                    SqlScoreboardEntry(
                        agent_id=agent_id,
                        metric=metric,
                        window=window,
                        value=value,
                        updated_at=now,
                    )
                )

    def scoreboard(
        self, *, metric: str, window: str = "all", limit: int = 10
    ) -> list[tuple[str, float]]:
        """Return ``(agent_id, value)`` ranked by value desc for a metric/window."""
        stmt = (
            select(SqlScoreboardEntry)
            .where(
                SqlScoreboardEntry.metric == metric,
                SqlScoreboardEntry.window == window,
            )
            .order_by(SqlScoreboardEntry.value.desc())
            .limit(limit)
        )
        with self._session() as session:
            return [(r.agent_id, r.value) for r in session.execute(stmt).scalars().all()]


_goal_store_cache: dict[str, SqlAlchemyGoalStore] = {}


def get_goal_store() -> SqlAlchemyGoalStore:
    """Return the durable goals/scoreboard store (BDP-2271 C3, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _goal_store_cache.get(location)
    if store is None:
        store = SqlAlchemyGoalStore(location)
        _goal_store_cache[location] = store
    return store
