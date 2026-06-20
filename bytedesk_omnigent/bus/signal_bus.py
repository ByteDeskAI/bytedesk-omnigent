"""Durable signal/await bus: deliver-by-id with idempotent replay + dead-letter
(BDP-2248, ADR-0142, aligned ADR-0009 Idempotent Receiver + Dead Letter Channel).

Omnigent is the sole engine; this replaces the ephemeral in-process inbox so a
wake survives a runner/process restart. The bus shares the conversation store's
database engine, exactly like ``SqlAlchemyMemoryStore`` (FU1, ADR-0132), and is
pure-DB / loop-agnostic so it can be unit-proven standalone before any runner
wiring (the runner wake-hook + the platform ``WorkflowSignalClient`` route layer
are separate follow-up tasks, ADR-0141).

Idempotency mechanism: ``signal_id`` is the ``pending_waits`` primary key, and
``deliver`` resolves it with a **guarded conditional UPDATE**
(``UPDATE pending_waits SET status='resolved' WHERE signal_id=:id AND
status='pending'``). The first deliver gets ``rowcount == 1`` → ``DELIVERED``;
a second deliver of the same id gets ``rowcount == 0`` → ``ALREADY_RESOLVED``.
An unmatched deliver (no row) is dead-lettered.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import func, select, update

from bytedesk_omnigent.db_models import SqlAgentMessage, SqlPendingWait
from bytedesk_omnigent.lifecycle import WaitKind, WaitStatus
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


class DeliveryStatus(str, Enum):
    """The outcome of a single ``deliver`` call."""

    DELIVERED = "delivered"  # signal was pending -> resolved + wake queued
    ALREADY_RESOLVED = "already_resolved"  # 2nd deliver of same signal_id (replay)
    DEAD_LETTERED = "dead_lettered"  # no pending wait ever registered (unmatched)
    EXPIRED = "expired"  # the wait expired before this (late) deliver -> dead-lettered


@dataclass(frozen=True)
class DeliveryResult:
    """Result of ``SqlAlchemySignalBus.deliver``."""

    status: DeliveryStatus
    signal_id: str
    session_id: str | None  # the parked session woken (None for DEAD_LETTERED)
    key: str | None  # the (session, key) the wait was registered under
    payload: dict[str, Any] | None  # echoed payload that was delivered


@dataclass(frozen=True)
class PendingWait:
    """A durable registered await (a row of ``pending_waits``)."""

    signal_id: str
    session_id: str
    key: str
    kind: WaitKind
    target: str | None
    status: WaitStatus
    created_at: int
    expires_at: int | None


def _to_pending_wait(row: SqlPendingWait) -> PendingWait:
    return PendingWait(
        signal_id=row.signal_id,
        session_id=row.session_id,
        key=row.key,
        kind=WaitKind(row.kind),
        target=row.target,
        status=WaitStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


class SqlAlchemySignalBus:
    """Durable signal/await bus (ADR-0142). See the module docstring.

    :param storage_location: SQLAlchemy database URI (the same engine the
        conversation store uses), e.g. ``"sqlite:///omnigent.db"`` or
        ``"postgresql+psycopg://user:pass@host/db"``.
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        # ``immediate=True`` acquires the SQLite write lock before any read, so
        # the deliver check-then-update cannot race (ADR-0009 single-writer);
        # a no-op on PostgreSQL, where the guarded UPDATE itself serialises.
        self._write_session = make_managed_session_maker(self._engine, immediate=True)
        self._is_sqlite = self._engine.dialect.name == "sqlite"

    @property
    def engine(self):
        """The underlying SQLAlchemy engine (used for advisory-lock coordination)."""
        return self._engine

    # ── PARK side ────────────────────────────────────────────────────
    def register_wait(
        self,
        *,
        signal_id: str,
        session_id: str,
        key: str,
        kind: str = "subscribe",
        target: str | None = None,
        expires_at: int | None = None,
        now: int | None = None,
    ) -> PendingWait:
        """Register a durable await keyed by ``signal_id``.

        Idempotent: re-registering an existing ``signal_id`` (recovery replay)
        is a no-op that returns the existing row.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            existing = session.get(SqlPendingWait, signal_id)
            if existing is not None:
                return _to_pending_wait(existing)
            row = SqlPendingWait(
                signal_id=signal_id,
                session_id=session_id,
                key=key,
                kind=kind,
                target=target,
                status="pending",
                created_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            return _to_pending_wait(row)

    def list_pending(
        self,
        *,
        kind: str | None = None,
        target: str | None = None,
        session_id: str | None = None,
    ) -> list[PendingWait]:
        """Return the still-``pending`` waits, optionally filtered.

        Backs ``GET /bytedesk/workflows/signals/pending?kind=&target=`` once the
        route layer is added (separate task).
        """
        stmt = select(SqlPendingWait).where(SqlPendingWait.status == "pending")
        if kind is not None:
            stmt = stmt.where(SqlPendingWait.kind == kind)
        if target is not None:
            stmt = stmt.where(SqlPendingWait.target == target)
        if session_id is not None:
            stmt = stmt.where(SqlPendingWait.session_id == session_id)
        stmt = stmt.order_by(SqlPendingWait.created_at)
        with self._session() as session:
            return [_to_pending_wait(r) for r in session.execute(stmt).scalars().all()]

    # ── DELIVER side ─────────────────────────────────────────────────
    def deliver(
        self,
        *,
        signal_id: str,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> DeliveryResult:
        """Deliver a signal by id; idempotent on replay; dead-letters unmatched.

        See the module docstring for the guarded-conditional-UPDATE idempotency
        contract.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            wait = session.get(SqlPendingWait, signal_id)
            if wait is None:
                # Dead Letter Channel: no wait was ever registered for this id.
                self._append_message(
                    session,
                    session_id=None,
                    signal_id=signal_id,
                    kind="dead_letter",
                    payload=payload,
                    dead_lettered=True,
                    now=now,
                )
                return DeliveryResult(
                    DeliveryStatus.DEAD_LETTERED, signal_id, None, None, payload
                )
            session_id = wait.session_id
            key = wait.key
            wait_kind = wait.kind
            result = session.execute(
                update(SqlPendingWait)
                .where(
                    SqlPendingWait.signal_id == signal_id,
                    SqlPendingWait.status == "pending",
                )
                .values(status="resolved", resolved_at=now)
            )
            if result.rowcount == 1:
                self._append_message(
                    session,
                    session_id=session_id,
                    signal_id=signal_id,
                    kind=wait_kind,
                    payload=payload,
                    dead_lettered=False,
                    now=now,
                )
                return DeliveryResult(
                    DeliveryStatus.DELIVERED, signal_id, session_id, key, payload
                )
            # The guarded UPDATE matched 0 rows — the wait is no longer pending.
            # Branch on its ACTUAL status (loaded above; unchanged by the no-op
            # UPDATE) instead of assuming a benign replay:
            #   - expired: the wait timed out before this late deliver, so the
            #     parked session was never woken. Dead-letter the payload so the
            #     signal is recoverable + report EXPIRED (NOT ALREADY_RESOLVED,
            #     which would tell the sender "handled, don't retry" while the
            #     signal is silently lost).
            #   - resolved: a true idempotent replay of an already-delivered signal.
            if wait.status == "expired":
                self._append_message(
                    session,
                    session_id=None,
                    signal_id=signal_id,
                    kind="expired",
                    payload=payload,
                    dead_lettered=True,
                    now=now,
                )
                return DeliveryResult(
                    DeliveryStatus.EXPIRED, signal_id, session_id, key, payload
                )
            return DeliveryResult(
                DeliveryStatus.ALREADY_RESOLVED, signal_id, session_id, key, payload
            )

    # ── WAKE side ────────────────────────────────────────────────────
    def drain_inbox(
        self, *, session_id: str, mark_read: bool = True, now: int | None = None
    ) -> list[dict]:
        """Return undelivered wake messages for ``session_id`` (FIFO by ``seq``).

        When ``mark_read`` is set, each returned row's ``delivered_at`` is
        stamped so a subsequent drain (e.g. after a reconnect) does not
        re-deliver it — the durable equivalent of the in-process delivered latch.
        Dead-letter rows are never drained into a session.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            rows = (
                session.execute(
                    select(SqlAgentMessage)
                    .where(
                        SqlAgentMessage.session_id == session_id,
                        SqlAgentMessage.delivered_at.is_(None),
                        SqlAgentMessage.dead_lettered.is_(False),
                    )
                    .order_by(SqlAgentMessage.seq)
                )
                .scalars()
                .all()
            )
            out: list[dict] = []
            for row in rows:
                out.append(
                    {
                        "id": row.id,
                        "seq": row.seq,
                        "kind": row.kind,
                        "signal_id": row.signal_id,
                        "payload": json.loads(row.payload),
                    }
                )
                if mark_read:
                    row.delivered_at = now
            return out

    def sweep_expired(self, *, now: int | None = None) -> int:
        """Reaper duty: mark ``pending`` waits past ``expires_at`` as ``expired``.

        Returns the number swept. Sibling of ``SqlAlchemyMemoryStore.sweep``;
        run on a timer under a PG advisory lock by ``signal_bus_reaper_loop``.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            result = session.execute(
                update(SqlPendingWait)
                .where(
                    SqlPendingWait.status == "pending",
                    SqlPendingWait.expires_at.is_not(None),
                    SqlPendingWait.expires_at < now,
                )
                .values(status="expired")
            )
            return result.rowcount or 0

    # ── internals ────────────────────────────────────────────────────
    def _append_message(
        self,
        session,
        *,
        session_id: str | None,
        signal_id: str | None,
        kind: str,
        payload: dict[str, Any] | None,
        dead_lettered: bool,
        now: int,
    ) -> None:
        next_seq = (
            session.execute(
                select(func.coalesce(func.max(SqlAgentMessage.seq), 0))
            ).scalar_one()
            + 1
        )
        session.add(
            SqlAgentMessage(
                id=f"am_{uuid.uuid4().hex}",
                seq=next_seq,
                session_id=session_id,
                signal_id=signal_id,
                kind=kind,
                payload=json.dumps(payload),
                dead_lettered=dead_lettered,
                created_at=now,
            )
        )
