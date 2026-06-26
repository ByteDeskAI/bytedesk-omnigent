from __future__ import annotations

import time

import pytest

from bytedesk_omnigent.fabric.outbox import (
    SCHEDULER_JOBS_SUBJECT,
    FabricOutboxPublisher,
    SqlAlchemyFabricOutboxStore,
    SqlOutboxSchedulerDispatch,
    scheduler_job_from_trigger,
)
from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler, run_cron_scheduler_tick
from bytedesk_omnigent.scheduler.scheduler import CronTrigger
from omnigent.fabric.models import FabricEnvelope, SchedulerJob


class _PublishingAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.envelopes: list[FabricEnvelope] = []

    async def publish_envelope(self, envelope: FabricEnvelope) -> None:
        if self.fail:
            raise RuntimeError("nats offline")
        self.envelopes.append(envelope)


def _uri(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'fabric.db'}"


def test_scheduler_job_from_trigger_uses_claim_check_payload_ref() -> None:
    trigger = CronTrigger(
        id="cron_1",
        agent_id="ag_maya",
        key="standup",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=123,
        enabled=True,
        payload={"tenant_id": "tenant_1", "org_id": "org_1", "lane": "priority"},
    )

    job = scheduler_job_from_trigger(trigger)

    assert job.idempotency_key == "schedule:cron_1:123"
    assert job.payload_ref == "sql:fabric_outbox:schedule:cron_1:123"
    assert job.fire_at_unix_ms == 123_000
    assert job.tenant_id == "tenant_1"
    assert job.org_id == "org_1"
    assert job.lane == "priority"
    assert job.metadata["agent_id"] == "ag_maya"


def test_claimed_cron_fire_writes_one_fabric_outbox_row(tmp_path) -> None:
    uri = _uri(tmp_path)
    scheduler = SqlAlchemyCronScheduler(uri)
    store = SqlAlchemyFabricOutboxStore(uri)
    now = int(time.time())
    trigger = scheduler.register_trigger(
        agent_id="ag_maya",
        key="standup",
        schedule_kind="interval",
        schedule_expr="60",
        next_fire_at=now,
        payload={"tenant_id": "tenant_1", "prompt": "Run standup."},
        now=now,
    )

    dispatch = SqlOutboxSchedulerDispatch(store)
    fired = run_cron_scheduler_tick(scheduler, dispatch, now=now)

    assert fired == 1
    pending = store.pending(now=now)
    assert len(pending) == 1
    record = pending[0]
    assert record.source == "scheduler"
    assert record.subject == SCHEDULER_JOBS_SUBJECT
    envelope = record.envelope()
    assert envelope.idempotency_key == f"schedule:{trigger.id}:{now}"
    assert envelope.payload_type == "scheduler_job"
    assert isinstance(envelope.payload, SchedulerJob)
    assert envelope.payload.schedule_id == trigger.id

    assert run_cron_scheduler_tick(scheduler, dispatch, now=now) == 0
    assert len(store.pending(now=now)) == 1


def test_outbox_enqueue_is_idempotent_by_message_key(tmp_path) -> None:
    store = SqlAlchemyFabricOutboxStore(_uri(tmp_path))
    dispatch = SqlOutboxSchedulerDispatch(store)
    job = SchedulerJob(
        job_id="sched_1",
        schedule_id="cron_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1_000,
        idempotency_key="schedule:cron_1:1",
        payload_ref="sql:fabric_outbox:schedule:cron_1:1",
    )

    first, inserted_first = dispatch.enqueue_job(job)
    second, inserted_second = dispatch.enqueue_job(job)

    assert inserted_first is True
    assert inserted_second is False
    assert second.id == first.id
    assert len(store.pending(now=2)) == 1


@pytest.mark.asyncio
async def test_outbox_publisher_publishes_and_marks_confirmed(tmp_path) -> None:
    store = SqlAlchemyFabricOutboxStore(_uri(tmp_path))
    dispatch = SqlOutboxSchedulerDispatch(store)
    job = SchedulerJob(
        job_id="sched_1",
        schedule_id="cron_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1_000,
        idempotency_key="schedule:cron_1:1",
        payload_ref="sql:fabric_outbox:schedule:cron_1:1",
    )
    record, _ = dispatch.enqueue_job(job)
    adapter = _PublishingAdapter()

    published = await FabricOutboxPublisher(store, adapter).replay_pending(now=10)

    assert published == 1
    assert adapter.envelopes[0].payload == job
    confirmed = store.get(record.id)
    assert confirmed is not None
    assert confirmed.status == "published"
    assert confirmed.published_at == 10
    assert store.pending(now=10) == []


@pytest.mark.asyncio
async def test_outbox_publisher_records_retry_and_dead_letters_at_cap(tmp_path) -> None:
    store = SqlAlchemyFabricOutboxStore(_uri(tmp_path))
    dispatch = SqlOutboxSchedulerDispatch(store)
    job = SchedulerJob(
        job_id="sched_1",
        schedule_id="cron_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1_000,
        idempotency_key="schedule:cron_1:1",
        payload_ref="sql:fabric_outbox:schedule:cron_1:1",
    )
    record, _ = dispatch.enqueue_job(job)
    publisher = FabricOutboxPublisher(
        store,
        _PublishingAdapter(fail=True),
        retry_delay_seconds=5,
        max_attempts=2,
    )

    assert await publisher.replay_pending(now=10) == 0
    failed = store.get(record.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.attempts == 1
    assert failed.next_attempt_at == 15
    assert "nats offline" in (failed.last_error or "")
    assert store.pending(now=14) == []

    assert await publisher.replay_pending(now=15) == 0
    dead = store.get(record.id)
    assert dead is not None
    assert dead.status == "dead_lettered"
    assert dead.attempts == 2
    assert dead.next_attempt_at is None


@pytest.mark.asyncio
async def test_fabric_extension_starts_outbox_replay_when_nats_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bytedesk_omnigent.fabric.extension import BytedeskFabricExtension

    seen: dict[str, object] = {}

    class _Registration:
        def __init__(self, service_name: str) -> None:
            self.service_name = service_name

        async def drain(self) -> None:
            seen.setdefault("drained", []).append(self.service_name)

    class _Adapter:
        def __init__(self, nats_url: str) -> None:
            seen["adapter_nats_url"] = nats_url

        async def ensure_assets(self, manifest) -> None:
            seen["asset_generation"] = manifest.generation

        async def serve_service_host(self, service) -> _Registration:
            seen.setdefault("services", []).append(service.name)
            return _Registration(service.name)

        async def close(self) -> None:
            seen["closed"] = True

    async def _fake_replay_loop(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setattr("omnigent.fabric.nats_adapter.NatsFabricAdapter", _Adapter)
    monkeypatch.setattr(
        "bytedesk_omnigent.fabric.outbox.fabric_outbox_replay_loop",
        _fake_replay_loop,
    )

    await BytedeskFabricExtension().fabric_service_background()

    assert seen["adapter_nats_url"] == "nats://127.0.0.1:4222"
    assert seen["asset_generation"] == 1
    assert "omnigent.fabric.control" in seen["services"]
    assert "omnigent.fabric.outbox" in seen["services"]
    assert seen["nats_url"] == "nats://127.0.0.1:4222"
    assert isinstance(seen["adapter"], _Adapter)
    assert seen["closed"] is True
    assert len(seen["drained"]) == len(seen["services"])
