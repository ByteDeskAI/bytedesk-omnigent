"""Durable native cron scheduler (BDP-2250, ADR-0142).

A single-writer schedule registry over the ``cron_triggers`` table, sharing the
conversation store's engine exactly like the durable signal bus (``omnigent/bus/``)
and the memory store (ADR-0132). The scheduler is pure-DB / loop-agnostic so it
can be unit-proven standalone; the ``_lifespan`` loop + the dispatch seam (open a
session + post the payload) are layered on top.

Exactly-once firing: ``claim_fire`` advances ``next_fire_at`` with a **guarded
UPDATE** on ``(id, next_fire_at)`` — the first claimer of a given fire instant
gets ``rowcount == 1`` and dispatches; a concurrent / replayed claim of the same
instant gets ``rowcount == 0`` and skips (ADR-0009 idempotency), the same shape
the signal bus uses for ``deliver``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update

from bytedesk_omnigent.db_models import SqlCronTrigger
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class CronTrigger:
    """A scheduled trigger (a row of ``cron_triggers``)."""

    id: str
    agent_id: str
    key: str
    schedule_kind: str  # "interval" | "cron" | "once"
    schedule_expr: str
    next_fire_at: int
    enabled: bool
    payload: dict | None


def compute_next_fire(schedule_kind: str, schedule_expr: str, after: int) -> int | None:
    """Compute the next fire instant after ``after`` (epoch seconds).

    - ``interval``: ``after + int(schedule_expr)`` (seconds).
    - ``once``: ``None`` — a one-shot trigger has no next fire (it is disabled
      after it fires).
    - ``cron``: deferred — cron-expression support requires ``croniter`` (not yet
      a dependency); use ``interval`` until it is added.
    """
    if schedule_kind == "interval":
        return after + int(schedule_expr)
    if schedule_kind == "once":
        return None
    if schedule_kind == "cron":
        raise NotImplementedError(
            "cron-expression schedules require croniter (not yet a dependency); "
            "use schedule_kind='interval' for now"
        )
    raise ValueError(f"unknown schedule_kind {schedule_kind!r}")


def _to_trigger(row: SqlCronTrigger) -> CronTrigger:
    return CronTrigger(
        id=row.id,
        agent_id=row.agent_id,
        key=row.key,
        schedule_kind=row.schedule_kind,
        schedule_expr=row.schedule_expr,
        next_fire_at=row.next_fire_at,
        enabled=row.enabled,
        payload=json.loads(row.payload) if row.payload is not None else None,
    )


class SqlAlchemyCronScheduler:
    """Durable native cron scheduler (ADR-0142). See the module docstring.

    :param storage_location: SQLAlchemy database URI (the same engine the
        conversation store uses).
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        # immediate=True: SQLite write-lock-before-read so claim_fire cannot race
        # (ADR-0009 single-writer); no-op on PostgreSQL.
        self._write_session = make_managed_session_maker(self._engine, immediate=True)
        self._is_sqlite = self._engine.dialect.name == "sqlite"

    @property
    def engine(self):
        """The underlying SQLAlchemy engine (used for advisory-lock coordination)."""
        return self._engine

    def register_trigger(
        self,
        *,
        agent_id: str,
        key: str,
        schedule_kind: str,
        schedule_expr: str,
        next_fire_at: int | None = None,
        payload: dict | None = None,
        now: int | None = None,
    ) -> CronTrigger:
        """Register (idempotently, by ``(agent_id, key)``) a scheduled trigger.

        Re-registering an existing ``(agent_id, key)`` updates its schedule +
        ``next_fire_at`` in place (so a redeploy reconciles a bundle's cadence
        without creating duplicates).
        """
        now = now_epoch() if now is None else now
        if next_fire_at is None:
            computed = compute_next_fire(schedule_kind, schedule_expr, now)
            next_fire_at = computed if computed is not None else now
        payload_json = json.dumps(payload) if payload is not None else None
        with self._write_session() as session:
            existing = session.execute(
                select(SqlCronTrigger).where(
                    SqlCronTrigger.agent_id == agent_id, SqlCronTrigger.key == key
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.schedule_kind = schedule_kind
                existing.schedule_expr = schedule_expr
                existing.next_fire_at = next_fire_at
                existing.payload = payload_json
                existing.enabled = True
                session.flush()
                return _to_trigger(existing)
            row = SqlCronTrigger(
                id=f"cron_{uuid.uuid4().hex}",
                agent_id=agent_id,
                key=key,
                schedule_kind=schedule_kind,
                schedule_expr=schedule_expr,
                next_fire_at=next_fire_at,
                enabled=True,
                payload=payload_json,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_trigger(row)

    def due_triggers(self, *, now: int | None = None) -> list[CronTrigger]:
        """Return enabled triggers whose ``next_fire_at <= now`` (FIFO by due)."""
        now = now_epoch() if now is None else now
        stmt = (
            select(SqlCronTrigger)
            .where(
                SqlCronTrigger.enabled.is_(True),
                SqlCronTrigger.next_fire_at <= now,
            )
            .order_by(SqlCronTrigger.next_fire_at)
        )
        with self._session() as session:
            return [_to_trigger(r) for r in session.execute(stmt).scalars().all()]

    def claim_fire(
        self,
        *,
        trigger_id: str,
        expected_next_fire_at: int,
        new_next_fire_at: int | None,
        now: int | None = None,
    ) -> bool:
        """Atomically claim a due fire and advance the schedule.

        Guarded UPDATE on ``(id, next_fire_at == expected_next_fire_at)``: the
        first caller for a given fire instant gets ``rowcount == 1`` (returns
        ``True`` → dispatch); a concurrent / replayed claim of the same instant
        gets ``rowcount == 0`` (returns ``False`` → skip). When
        ``new_next_fire_at`` is ``None`` (a one-shot ``once`` trigger) the trigger
        is disabled instead of advanced.
        """
        now = now_epoch() if now is None else now
        if new_next_fire_at is None:
            values = {"enabled": False, "last_fired_at": now}
        else:
            values = {"next_fire_at": new_next_fire_at, "last_fired_at": now}
        with self._write_session() as session:
            result = session.execute(
                update(SqlCronTrigger)
                .where(
                    SqlCronTrigger.id == trigger_id,
                    SqlCronTrigger.next_fire_at == expected_next_fire_at,
                )
                .values(**values)
            )
            return result.rowcount == 1


def run_cron_scheduler_tick(scheduler, dispatch, *, now: int | None = None) -> int:
    """Find due triggers, claim each exactly-once, and dispatch the claimed ones.

    Returns the number dispatched. ``dispatch`` is an injectable callable
    ``(CronTrigger) -> None`` so the tick is unit-testable without a live
    session/runner; the ``_lifespan`` loop passes the real session-opening
    dispatch. A trigger that loses the claim race (another worker / replica
    already advanced its fire instant) is skipped.
    """
    now = now_epoch() if now is None else now
    fired = 0
    for trig in scheduler.due_triggers(now=now):
        new_next = compute_next_fire(trig.schedule_kind, trig.schedule_expr, now)
        claimed = scheduler.claim_fire(
            trigger_id=trig.id,
            expected_next_fire_at=trig.next_fire_at,
            new_next_fire_at=new_next,
            now=now,
        )
        if not claimed:
            continue
        dispatch(trig)
        fired += 1
    return fired
