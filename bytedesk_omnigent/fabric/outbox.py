"""SQL-to-NATS outbox for the ByteDesk fabric cutover."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.db_models import SqlFabricOutbox
from bytedesk_omnigent.maintenance import advisory_locked_loop
from bytedesk_omnigent.scheduler.scheduler import CronTrigger
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.fabric.models import (
    AuditEvent,
    CapacityRecord,
    CredentialReference,
    DlqRecord,
    FabricEnvelope,
    LeaseRecord,
    LifecycleEvent,
    PlacementDecision,
    RunnerHeartbeat,
    RunnerJob,
    SchedulerJob,
    TimelineEvent,
)
from omnigent.fabric.nats_adapter import NatsFabricAdapter

_logger = logging.getLogger(__name__)

SCHEDULER_JOBS_SUBJECT = "omnigent.scheduler.jobs"

_OUTBOX_LOCK_KEY = 0x6661626F75746278
_DEFAULT_REPLAY_INTERVAL_SECONDS = 5
_DEFAULT_RETRY_DELAY_SECONDS = 30
_DEFAULT_MAX_ATTEMPTS = 10

_PAYLOAD_TYPES: dict[str, type] = {
    "audit_event": AuditEvent,
    "capacity_record": CapacityRecord,
    "credential_reference": CredentialReference,
    "dlq_record": DlqRecord,
    "lease_record": LeaseRecord,
    "lifecycle_event": LifecycleEvent,
    "placement_decision": PlacementDecision,
    "runner_heartbeat": RunnerHeartbeat,
    "runner_job": RunnerJob,
    "scheduler_job": SchedulerJob,
    "timeline_event": TimelineEvent,
}


@dataclass(frozen=True)
class FabricOutboxRecord:
    id: str
    idempotency_key: str
    source: str
    subject: str
    payload_type: str
    payload: str
    status: str
    attempts: int
    last_error: str | None
    next_attempt_at: int | None
    published_at: int | None
    created_at: int
    updated_at: int
    metadata: dict[str, Any]

    def envelope(self) -> FabricEnvelope:
        payload_type = _PAYLOAD_TYPES.get(self.payload_type, dict)
        return FabricEnvelope.from_json(self.payload, payload_type)

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "source": self.source,
            "subject": self.subject,
            "payload_type": self.payload_type,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "next_attempt_at": self.next_attempt_at,
            "published_at": self.published_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


def _loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _to_record(row: SqlFabricOutbox) -> FabricOutboxRecord:
    return FabricOutboxRecord(
        id=row.id,
        idempotency_key=row.idempotency_key,
        source=row.source,
        subject=row.subject,
        payload_type=row.payload_type,
        payload=row.payload,
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        published_at=row.published_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=_loads(row.meta),
    )


class SqlAlchemyFabricOutboxStore:
    """Durable outbox backing fabric publishes."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def enqueue_envelope(
        self,
        envelope: FabricEnvelope,
        *,
        source: str,
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> tuple[FabricOutboxRecord, bool]:
        """Insert an envelope idempotently by ``idempotency_key``."""
        now = now_epoch() if now is None else now
        try:
            with self._write_session() as session:
                row = SqlFabricOutbox(
                    id=f"fout_{uuid.uuid4().hex}",
                    idempotency_key=envelope.idempotency_key,
                    source=source,
                    subject=envelope.subject,
                    payload_type=envelope.payload_type,
                    payload=envelope.to_json().decode("utf-8"),
                    status="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                    meta=json.dumps(metadata or {}),
                )
                session.add(row)
                session.flush()
                return _to_record(row), True
        except IntegrityError:
            existing = self.get_by_idempotency_key(envelope.idempotency_key)
            if existing is None:
                raise
            return existing, False

    def pending(self, *, limit: int = 100, now: int | None = None) -> list[FabricOutboxRecord]:
        """Rows due for publish or retry, oldest first."""
        now = now_epoch() if now is None else now
        stmt = (
            select(SqlFabricOutbox)
            .where(
                SqlFabricOutbox.status.in_(("pending", "failed")),
                or_(
                    SqlFabricOutbox.next_attempt_at.is_(None),
                    SqlFabricOutbox.next_attempt_at <= now,
                ),
            )
            .order_by(SqlFabricOutbox.created_at, SqlFabricOutbox.id)
            .limit(limit)
        )
        with self._session() as session:
            return [_to_record(row) for row in session.execute(stmt).scalars().all()]

    def recent(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[FabricOutboxRecord]:
        stmt = select(SqlFabricOutbox)
        if status is not None:
            stmt = stmt.where(SqlFabricOutbox.status == status)
        stmt = stmt.order_by(SqlFabricOutbox.created_at.desc(), SqlFabricOutbox.id).limit(limit)
        with self._session() as session:
            return [_to_record(row) for row in session.execute(stmt).scalars().all()]

    def get(self, outbox_id: str) -> FabricOutboxRecord | None:
        with self._session() as session:
            row = session.get(SqlFabricOutbox, outbox_id)
            return _to_record(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> FabricOutboxRecord | None:
        stmt = select(SqlFabricOutbox).where(
            SqlFabricOutbox.idempotency_key == idempotency_key
        )
        with self._session() as session:
            row = session.execute(stmt).scalar_one_or_none()
            return _to_record(row) if row is not None else None

    def mark_published(self, outbox_id: str, *, now: int | None = None) -> None:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlFabricOutbox, outbox_id)
            if row is None:
                return
            row.status = "published"
            row.last_error = None
            row.next_attempt_at = None
            row.published_at = now
            row.updated_at = now

    def mark_failed(
        self,
        outbox_id: str,
        *,
        error: str,
        now: int | None = None,
        retry_delay_seconds: int = _DEFAULT_RETRY_DELAY_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlFabricOutbox, outbox_id)
            if row is None or row.status in {"published", "dead_lettered"}:
                return
            row.attempts = row.attempts + 1
            row.last_error = error
            row.updated_at = now
            if row.attempts >= max_attempts:
                row.status = "dead_lettered"
                row.next_attempt_at = None
            else:
                row.status = "failed"
                row.next_attempt_at = now + retry_delay_seconds


class SqlOutboxSchedulerDispatch:
    """Cron-scheduler dispatch adapter that writes SchedulerJob envelopes."""

    def __init__(
        self,
        store: SqlAlchemyFabricOutboxStore,
        *,
        subject: str = SCHEDULER_JOBS_SUBJECT,
    ) -> None:
        self._store = store
        self._subject = subject

    def __call__(self, trigger: CronTrigger) -> None:
        job = scheduler_job_from_trigger(trigger)
        record, inserted = self.enqueue_job(job)
        if inserted:
            _logger.info(
                "fabric scheduler outbox: enqueued schedule=%s key=%s outbox=%s",
                job.schedule_id,
                job.idempotency_key,
                record.id,
            )

    async def dispatch(self, job: SchedulerJob) -> None:
        await asyncio.to_thread(self.enqueue_job, job)

    def enqueue_job(self, job: SchedulerJob) -> tuple[FabricOutboxRecord, bool]:
        envelope = FabricEnvelope.wrap(
            subject=self._subject,
            idempotency_key=job.idempotency_key,
            payload=job,
        )
        return self._store.enqueue_envelope(
            envelope,
            source="scheduler",
            metadata={
                "schedule_id": job.schedule_id,
                "job_id": job.job_id,
                "lane": job.lane,
            },
        )


class FabricOutboxPublisher:
    """Replay SQL outbox rows through the NATS fabric adapter."""

    def __init__(
        self,
        store: SqlAlchemyFabricOutboxStore,
        adapter: NatsFabricAdapter,
        *,
        retry_delay_seconds: int = _DEFAULT_RETRY_DELAY_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._retry_delay_seconds = retry_delay_seconds
        self._max_attempts = max_attempts

    async def replay_pending(self, *, limit: int = 100, now: int | None = None) -> int:
        now = now_epoch() if now is None else now
        records = await asyncio.to_thread(self._store.pending, limit=limit, now=now)
        published = 0
        for record in records:
            try:
                await self._adapter.publish_envelope(record.envelope())
            except Exception as exc:  # noqa: BLE001 - one bad row must not block replay
                await asyncio.to_thread(
                    self._store.mark_failed,
                    record.id,
                    error=str(exc),
                    now=now,
                    retry_delay_seconds=self._retry_delay_seconds,
                    max_attempts=self._max_attempts,
                )
                continue
            await asyncio.to_thread(self._store.mark_published, record.id, now=now)
            published += 1
        return published


def scheduler_job_from_trigger(trigger: CronTrigger) -> SchedulerJob:
    payload = trigger.payload or {}
    tenant_id = str(payload.get("tenant_id") or payload.get("tenantId") or "default")
    org_id = str(payload.get("org_id") or payload.get("orgId") or tenant_id)
    lane = str(payload.get("lane") or "default")
    idempotency_key = f"schedule:{trigger.id}:{trigger.next_fire_at}"
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return SchedulerJob(
        job_id=f"sched_{digest}",
        schedule_id=trigger.id,
        tenant_id=tenant_id,
        org_id=org_id,
        lane=lane,
        fire_at_unix_ms=trigger.next_fire_at * 1000,
        idempotency_key=idempotency_key,
        payload_ref=f"sql:fabric_outbox:{idempotency_key}",
        metadata={
            "agent_id": trigger.agent_id,
            "trigger_key": trigger.key,
            "schedule_kind": str(trigger.schedule_kind),
            "schedule_expr": trigger.schedule_expr,
            "has_payload": bool(trigger.payload),
        },
    )


def build_fabric_cron_dispatch(
    store: SqlAlchemyFabricOutboxStore | None = None,
) -> SqlOutboxSchedulerDispatch:
    return SqlOutboxSchedulerDispatch(store or get_fabric_outbox_store())


async def fabric_outbox_replay_loop(
    *,
    nats_url: str,
    store: SqlAlchemyFabricOutboxStore | None = None,
    adapter: NatsFabricAdapter | None = None,
    interval_seconds: int = _DEFAULT_REPLAY_INTERVAL_SECONDS,
    lock_key: int = _OUTBOX_LOCK_KEY,
) -> None:
    close_adapter = adapter is None
    resolved_store = store or get_fabric_outbox_store()
    resolved_adapter = adapter or NatsFabricAdapter(nats_url)
    publisher = FabricOutboxPublisher(resolved_store, resolved_adapter)

    def _prepare():
        async def _work() -> None:
            published = await publisher.replay_pending()
            if published:
                _logger.info("fabric outbox: published=%d", published)

        return resolved_store.engine, _work

    try:
        await advisory_locked_loop(
            interval_seconds=interval_seconds,
            lock_key=lock_key,
            prepare=_prepare,
            logger=_logger,
            name="fabric outbox",
        )
    finally:
        if close_adapter:
            await resolved_adapter.close()


_store_cache: dict[str, SqlAlchemyFabricOutboxStore] = {}


def get_fabric_outbox_store() -> SqlAlchemyFabricOutboxStore:
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _store_cache.get(location)
    if store is None:
        store = SqlAlchemyFabricOutboxStore(location)
        _store_cache[location] = store
    return store
