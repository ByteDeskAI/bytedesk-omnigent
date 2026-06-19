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
    updated_at: int


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
        updated_at=row.updated_at,
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
        """Move a goal to a new status (``in_progress`` / ``blocked`` / ``done`` …).

        Re-arms accountability escalation on every (re-)transition to ``blocked``
        by resetting ``escalated_at`` to NULL, so a goal that is unblocked and
        later re-blocked escalates once again (BDP-2283).
        """
        now = now_epoch() if now is None else now
        values: dict = {"status": status, "updated_at": now}
        if status == "blocked":
            values["escalated_at"] = None
        with self._write_session() as session:
            session.execute(
                update(SqlGoal).where(SqlGoal.id == goal_id).values(**values)
            )

    def advance_goal_owned(
        self, *, goal_id: str, status: str, owner_agent_id: str, now: int | None = None
    ) -> bool:
        """Move a goal the caller OWNS to a new status (BDP-2285 authz).

        Guarded UPDATE on ``(id, owner_agent_id)`` — an agent can only advance its
        OWN goal, and a missing / foreign goal matches 0 rows. Returns ``True``
        only when this owner's goal was advanced (``rowcount == 1``), so the
        agent-tool never reports fabricated success for a goal it doesn't own or
        that doesn't exist. ``open`` clears the owner (it returns to the backlog).
        """
        now = now_epoch() if now is None else now
        values: dict = {"status": status, "updated_at": now}
        if status == "blocked":
            values["escalated_at"] = None
        if status == "open":
            values["owner_agent_id"] = None
        with self._write_session() as session:
            result = session.execute(
                update(SqlGoal)
                .where(
                    SqlGoal.id == goal_id,
                    SqlGoal.owner_agent_id == owner_agent_id,
                )
                .values(**values)
            )
            return result.rowcount == 1

    def escalate_blocked(self, *, now: int | None = None) -> list[Goal]:
        """Claim not-yet-escalated ``blocked`` goals, marking them escalated (C4).

        The accountability loop calls this each tick: it returns the blocked goals
        whose ``escalated_at`` is still NULL (stamping it to ``now``) so the caller
        sends exactly one escalation per blocked episode — a re-tick returns ``[]``
        (no spam), and a goal re-entering ``blocked`` (``advance_goal`` reset its
        ``escalated_at``) is returned again. Single guarded write (ADR-0009).
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            rows = (
                session.execute(
                    select(SqlGoal).where(
                        SqlGoal.status == "blocked",
                        SqlGoal.escalated_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            snapshot = [_to_goal(r) for r in rows]
            for row in rows:
                row.escalated_at = now
            session.flush()
            return snapshot

    def reopen_stalled(
        self, *, older_than_seconds: int, now: int | None = None
    ) -> list[Goal]:
        """Rebalance: reopen owned goals idle past ``older_than_seconds`` (BDP-2272 C4).

        An ``assigned`` / ``in_progress`` goal whose ``updated_at`` is older than the
        threshold is stalled — its owner is sitting on it. Reopen it (``status='open'``,
        ``owner_agent_id=None``) so another agent can claim it. Returns the goals **as
        they were before reopening** (carrying the prior ``owner_agent_id``) so the
        caller can notify the dropped owner. Single guarded write (ADR-0009).
        """
        now = now_epoch() if now is None else now
        cutoff = now - older_than_seconds
        with self._write_session() as session:
            rows = (
                session.execute(
                    select(SqlGoal).where(
                        SqlGoal.status.in_(("assigned", "in_progress")),
                        SqlGoal.updated_at <= cutoff,
                    )
                )
                .scalars()
                .all()
            )
            reopened = [_to_goal(r) for r in rows]  # snapshot prior owner
            for r in rows:
                r.status = "open"
                r.owner_agent_id = None
                r.updated_at = now
            session.flush()
            return reopened

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
