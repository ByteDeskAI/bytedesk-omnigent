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
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update

from bytedesk_omnigent.db_models import SqlCronTrigger
from bytedesk_omnigent.lifecycle import ScheduleKind
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
    schedule_kind: ScheduleKind  # interval | cron | once
    schedule_expr: str
    next_fire_at: int
    enabled: bool
    payload: dict | None
    # Monotonic optimistic-concurrency ETag (BDP-2412) — If-Match on re-register.
    version: int = 1


# ── Schedule-kind Strategy registry (BDP-2349 #13) ──────────────────────────
# Each strategy maps ``(schedule_expr, after) -> next_fire | None`` for one
# schedule_kind. Replaces the closed if/elif so a new cadence (rrule) is a
# `register_schedule_kind` call, not an edit to this function.
ScheduleKindStrategy = Callable[[str, int], "int | None"]


def _interval_strategy(schedule_expr: str, after: int) -> int | None:
    """``interval``: fire every ``int(schedule_expr)`` seconds."""
    return after + int(schedule_expr)


def _once_strategy(schedule_expr: str, after: int) -> int | None:
    """``once``: a one-shot trigger has no next fire (disabled after it fires)."""
    del schedule_expr, after
    return None


def _cron_strategy(schedule_expr: str, after: int) -> int | None:
    """``cron``: compute the next five-field cron occurrence with ``croniter``."""
    from croniter import croniter

    return int(croniter(schedule_expr, after).get_next(float))


_SCHEDULE_KIND_STRATEGIES: dict[str, ScheduleKindStrategy] = {
    "interval": _interval_strategy,
    "once": _once_strategy,
    "cron": _cron_strategy,
}


def register_schedule_kind(kind: str, strategy: ScheduleKindStrategy) -> None:
    """Register a ``(schedule_expr, after) -> next_fire | None`` *strategy* for *kind*.

    The seam for new cadences (e.g. ``cron``/``rrule`` once croniter is a
    dependency) — register a strategy instead of editing :func:`compute_next_fire`.
    """
    _SCHEDULE_KIND_STRATEGIES[kind] = strategy


def compute_next_fire(schedule_kind: str, schedule_expr: str, after: int) -> int | None:
    """Compute the next fire instant after ``after`` (epoch seconds).

    Dispatches to the registered schedule-kind strategy:

    - ``interval``: ``after + int(schedule_expr)`` (seconds).
    - ``once``: ``None`` — a one-shot trigger has no next fire (it is disabled
      after it fires).
    - ``cron``: the next occurrence of a five-field cron expression.

    :raises ValueError: if no strategy is registered for *schedule_kind*.
    """
    try:
        strategy = _SCHEDULE_KIND_STRATEGIES[schedule_kind]
    except KeyError:
        raise ValueError(f"unknown schedule_kind {schedule_kind!r}") from None
    return strategy(schedule_expr, after)


def _to_trigger(row: SqlCronTrigger) -> CronTrigger:
    return CronTrigger(
        id=row.id,
        agent_id=row.agent_id,
        key=row.key,
        schedule_kind=ScheduleKind(row.schedule_kind),
        schedule_expr=row.schedule_expr,
        next_fire_at=row.next_fire_at,
        enabled=row.enabled,
        payload=json.loads(row.payload) if row.payload is not None else None,
        version=row.version,
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
        expected_version: int | None = None,
    ) -> CronTrigger:
        """Register (idempotently, by ``(agent_id, key)``) a scheduled trigger.

        Re-registering an existing ``(agent_id, key)`` updates its schedule +
        ``next_fire_at`` in place (so a redeploy reconciles a bundle's cadence
        without creating duplicates).

        Optimistic concurrency (BDP-2412): when *expected_version* is given AND
        the trigger already exists, the in-place update is a guarded
        compare-and-swap on ``version`` (raises ``StaleWriteError`` on a stale
        ETag). It is ignored on first registration (an INSERT has no prior
        version). Omitted keeps the unconditional re-register; either way an
        in-place update bumps ``version``.
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
                if expected_version is None:
                    existing.schedule_kind = schedule_kind
                    existing.schedule_expr = schedule_expr
                    existing.next_fire_at = next_fire_at
                    existing.payload = payload_json
                    existing.enabled = True
                    existing.version = existing.version + 1
                    session.flush()
                    return _to_trigger(existing)
                from omnigent.errors import StaleWriteError

                result = session.execute(
                    update(SqlCronTrigger)
                    .where(
                        SqlCronTrigger.id == existing.id,
                        SqlCronTrigger.version == expected_version,
                    )
                    .values(
                        schedule_kind=schedule_kind,
                        schedule_expr=schedule_expr,
                        next_fire_at=next_fire_at,
                        payload=payload_json,
                        enabled=True,
                        version=SqlCronTrigger.version + 1,
                    )
                )
                if result.rowcount == 1:
                    session.expire_all()
                    return _to_trigger(session.get(SqlCronTrigger, existing.id))
                raise StaleWriteError(
                    f"cron trigger {existing.id!r} was modified concurrently "
                    f"(If-Match version {expected_version} is stale)"
                )
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
                version=1,
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

    def list_triggers(
        self,
        *,
        agent_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[CronTrigger]:
        """List registered triggers by next fire time, optionally scoped."""
        stmt = select(SqlCronTrigger)
        if agent_id is not None:
            stmt = stmt.where(SqlCronTrigger.agent_id == agent_id)
        if enabled is not None:
            stmt = stmt.where(SqlCronTrigger.enabled.is_(enabled))
        stmt = stmt.order_by(SqlCronTrigger.next_fire_at, SqlCronTrigger.key)
        with self._session() as session:
            return [_to_trigger(r) for r in session.execute(stmt).scalars().all()]

    def get_trigger(self, trigger_id: str) -> CronTrigger | None:
        """Return one trigger by id, or ``None`` when missing."""
        with self._session() as session:
            row = session.get(SqlCronTrigger, trigger_id)
            return _to_trigger(row) if row is not None else None

    def set_enabled(self, *, trigger_id: str, enabled: bool) -> bool:
        """Enable or disable a trigger. Returns True iff a row was updated."""
        with self._write_session() as session:
            result = session.execute(
                update(SqlCronTrigger)
                .where(SqlCronTrigger.id == trigger_id)
                .values(enabled=enabled, version=SqlCronTrigger.version + 1)
            )
            return result.rowcount == 1

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
