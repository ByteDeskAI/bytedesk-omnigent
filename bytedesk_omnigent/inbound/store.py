"""Durable inbound-event Message Store (ADR-0155, BDP-2559).

The **Wire-Tap log** + **Idempotent Receiver** guard in one table: ``record_event``
INSERTs the canonical event keyed by ``idempotency_key`` (PK) — a replay raises
``IntegrityError`` and short-circuits as a duplicate. ``inbound_event_results`` holds
per-processor fan-out outcomes + Dead-Letter retry state. Mirrors the
``SqlAlchemyAgenticInboxEventStore`` shape (engine, write-session, module ``_store_cache``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.db_models import SqlInboundEvent, SqlInboundEventResult
from bytedesk_omnigent.inbound.event import InboundEvent
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)

EVENT_STATUSES = ("received", "fanned_out", "duplicate", "dead_lettered")
RESULT_STATUSES = ("ok", "skipped", "failed", "dead_lettered")


def _loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True)
class InboundEventRecord:
    idempotency_key: str
    source: str
    type: str
    status: str
    occurred_at: int
    received_at: int
    tenant_id: str | None
    event_id: str | None
    raw_payload: dict[str, Any]
    normalized: dict[str, Any]
    headers: dict[str, str]
    attempts: int
    error: str | None
    created_at: int
    updated_at: int


def _to_record(row: SqlInboundEvent) -> InboundEventRecord:
    return InboundEventRecord(
        idempotency_key=row.idempotency_key,
        source=row.source,
        type=row.type,
        status=row.status,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        tenant_id=row.tenant_id,
        event_id=row.event_id,
        raw_payload=_loads(row.raw_payload),
        normalized=_loads(row.normalized),
        headers=_loads(row.headers),
        attempts=row.attempts,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def record_to_event(record: InboundEventRecord) -> InboundEvent:
    """Rebuild a canonical :class:`InboundEvent` from a stored record (reaper replay)."""
    return InboundEvent(
        idempotency_key=record.idempotency_key,
        source=record.source,
        type=record.type,
        occurred_at=record.occurred_at,
        received_at=record.received_at,
        raw_payload=record.raw_payload,
        normalized=record.normalized,
        headers=record.headers,
        tenant_id=record.tenant_id,
        event_id=record.event_id,
    )


class SqlAlchemyInboundEventStore:
    """SQLAlchemy-backed inbound-event Message Store (ADR-0155)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    # -- events --------------------------------------------------------
    def record_event(
        self, event: InboundEvent, *, now: int | None = None
    ) -> tuple[InboundEventRecord, bool]:
        """Wire-Tap + Idempotent-Receiver claim. Returns ``(record, inserted)``;
        ``inserted=False`` means a duplicate (the key was already seen)."""
        now = now_epoch() if now is None else now
        try:
            with self._write_session() as session:
                row = SqlInboundEvent(
                    idempotency_key=event.idempotency_key,
                    source=event.source,
                    type=event.type,
                    tenant_id=event.tenant_id,
                    event_id=event.event_id,
                    occurred_at=event.occurred_at,
                    received_at=event.received_at,
                    status="received",
                    raw_payload=json.dumps(event.raw_payload),
                    normalized=json.dumps(event.normalized),
                    headers=json.dumps(event.headers),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return _to_record(row), True
        except IntegrityError:
            existing = self.get(event.idempotency_key)
            if existing is None:
                raise
            return existing, False

    def mark_status(
        self, idempotency_key: str, status: str, *, now: int | None = None
    ) -> None:
        """Advance the event's lifecycle status (received → fanned_out / dead_lettered)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            session.execute(
                update(SqlInboundEvent)
                .where(SqlInboundEvent.idempotency_key == idempotency_key)
                .values(status=status, updated_at=now)
            )

    def get(self, idempotency_key: str) -> InboundEventRecord | None:
        with self._session() as session:
            row = session.get(SqlInboundEvent, idempotency_key)
            return _to_record(row) if row is not None else None

    def recent(self, *, limit: int = 100) -> list[InboundEventRecord]:
        """Most-recent events (newest first) — REST snapshot hydration for the feed."""
        stmt = select(SqlInboundEvent).order_by(SqlInboundEvent.received_at.desc()).limit(limit)
        with self._session() as session:
            return [_to_record(r) for r in session.execute(stmt).scalars().all()]

    # -- per-processor results -----------------------------------------
    def record_result(
        self,
        *,
        idempotency_key: str,
        processor: str,
        status: str,
        error: str | None = None,
        next_retry_at: int | None = None,
        now: int | None = None,
    ) -> None:
        """Upsert the outcome of one processor handling one event (by ``(key, processor)``)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            existing = session.execute(
                select(SqlInboundEventResult).where(
                    SqlInboundEventResult.idempotency_key == idempotency_key,
                    SqlInboundEventResult.processor == processor,
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.status = status
                existing.error = error
                existing.next_retry_at = next_retry_at
                existing.attempts += 1
                existing.updated_at = now
            else:
                session.add(
                    SqlInboundEventResult(
                        id=f"inres_{uuid.uuid4().hex}",
                        idempotency_key=idempotency_key,
                        processor=processor,
                        status=status,
                        error=error,
                        next_retry_at=next_retry_at,
                        attempts=1,
                        created_at=now,
                        updated_at=now,
                    )
                )

    def due_retries(self, *, now: int | None = None) -> list[tuple[str, str, int]]:
        """Return ``(idempotency_key, processor, attempts)`` for failed results whose
        ``next_retry_at`` has passed — the Dead-Letter reaper's work list."""
        now = now_epoch() if now is None else now
        stmt = select(SqlInboundEventResult).where(
            SqlInboundEventResult.status == "failed",
            SqlInboundEventResult.next_retry_at.is_not(None),
            SqlInboundEventResult.next_retry_at <= now,
        )
        with self._session() as session:
            return [
                (r.idempotency_key, r.processor, r.attempts)
                for r in session.execute(stmt).scalars().all()
            ]


_store_cache: dict[str, SqlAlchemyInboundEventStore] = {}


def get_inbound_event_store() -> SqlAlchemyInboundEventStore:
    """Return the durable inbound-event Message Store (ADR-0155)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _store_cache.get(location)
    if store is None:
        store = SqlAlchemyInboundEventStore(location)
        _store_cache[location] = store
    return store
