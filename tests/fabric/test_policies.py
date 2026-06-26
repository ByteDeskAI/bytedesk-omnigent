from __future__ import annotations

import pytest

from omnigent.fabric.models import CredentialReference, DlqRecord, RunnerJob
from omnigent.fabric.policies import (
    FabricCapacityRejected,
    InMemoryFabricCapacityPolicy,
    InMemoryFabricRecoveryPolicy,
    InMemoryQuarantinePolicy,
    ReplaySimulationRequired,
    WarmFirstPlacementStrategy,
)


def _credential() -> CredentialReference:
    return CredentialReference(
        ref="cred_1",
        principal_id="runner",
        expires_unix_ms=2_000,
    )


def _runner_job(
    *,
    job_id: str = "job_1",
    tenant_id: str = "tenant_1",
    lane: str = "default",
    epoch: int = 1,
) -> RunnerJob:
    return RunnerJob(
        job_id=job_id,
        session_id=f"conv_{job_id}",
        tenant_id=tenant_id,
        org_id="org_1",
        lane=lane,
        epoch=epoch,
        deadline_unix_ms=2_000,
        capacity={"cpu": 1},
        credential=_credential(),
        idempotency_key=f"runner:{job_id}",
    )


def _dlq_record() -> DlqRecord:
    return DlqRecord(
        dlq_id="dlq_1",
        source_subject="omnigent.runner.jobs.default",
        source_stream="OMNIGENT_RUNNER_JOBS",
        idempotency_key="runner:job_1",
        reason="max-delivery",
        payload_ref="object://replay/job_1",
        failed_unix_ms=1_000,
        deliveries=5,
    )


@pytest.mark.asyncio
async def test_capacity_policy_rejects_budget_then_releases() -> None:
    capacity = InMemoryFabricCapacityPolicy(
        tenant_limits={"tenant_1": 1},
        lane_limits={"default": 2},
        now_ms=lambda: 1_000,
    )
    first = _runner_job(job_id="job_1")
    second = _runner_job(job_id="job_2")

    reservation = await capacity.reserve(first)

    with pytest.raises(FabricCapacityRejected) as rejected:
        await capacity.reserve(second)
    assert rejected.value.scope == "tenant"
    assert rejected.value.key == "tenant_1"

    await capacity.release(reservation)
    await capacity.reserve(second)

    records = {(record.scope, record.key): record for record in capacity.records()}
    assert records[("tenant", "tenant_1")].used == 1
    assert records[("lane", "default")].used == 1


@pytest.mark.asyncio
async def test_placement_uses_warm_runner_then_cold_spawn() -> None:
    strategy = WarmFirstPlacementStrategy(
        capacity=InMemoryFabricCapacityPolicy(now_ms=lambda: 1_000),
        warm_runners={"default": ["runner_warm"]},
        host_ids=["host_1"],
        now_ms=lambda: 1_000,
    )

    warm = await strategy.place(_runner_job(job_id="job_warm"))
    cold = await strategy.place(_runner_job(job_id="job_cold", epoch=2))

    assert warm.mode == "warm_hit"
    assert warm.runner_id == "runner_warm"
    assert cold.mode == "cold_spawn"
    assert cold.host_id == "host_1"


@pytest.mark.asyncio
async def test_placement_rejects_quarantined_lane_without_capacity_reservation() -> None:
    capacity = InMemoryFabricCapacityPolicy(now_ms=lambda: 1_000)
    quarantine = InMemoryQuarantinePolicy(now_ms=lambda: 1_000)
    quarantine.apply(resource_type="lane", resource_id="default", reason="timeout")
    strategy = WarmFirstPlacementStrategy(
        capacity=capacity,
        quarantine=quarantine,
        host_ids=["host_1"],
        now_ms=lambda: 1_000,
    )

    decision = await strategy.place(_runner_job(job_id="job_1"))

    assert decision.mode == "rejected"
    assert decision.reason == "lane quarantined"
    assert capacity.records() == []


def test_quarantine_policy_applies_after_repeated_failures() -> None:
    quarantine = InMemoryQuarantinePolicy(threshold=2, now_ms=lambda: 1_000)

    assert (
        quarantine.record_failure(
            resource_type="runner",
            resource_id="runner_1",
            reason="crash",
        )
        is None
    )
    record = quarantine.record_failure(
        resource_type="runner",
        resource_id="runner_1",
        reason="crash",
        metadata={"host_id": "host_1"},
    )

    assert record is not None
    assert record.failures == 2
    assert record.metadata["host_id"] == "host_1"
    assert quarantine.is_quarantined("runner", "runner_1") is True

    quarantine.release(resource_type="runner", resource_id="runner_1")

    assert quarantine.is_quarantined("runner", "runner_1") is False


@pytest.mark.asyncio
async def test_recovery_policy_requires_simulation_before_replay() -> None:
    published: list[str] = []

    async def _publish(record: DlqRecord) -> None:
        published.append(record.dlq_id)

    recovery = InMemoryFabricRecoveryPolicy(publisher=_publish)
    record = _dlq_record()

    with pytest.raises(ReplaySimulationRequired):
        await recovery.replay(record)

    operations = await recovery.simulate_replay(record)
    await recovery.replay(record)

    assert operations == [
        "load claim-check payload object://replay/job_1",
        "republish runner:job_1 to omnigent.runner.jobs.default",
        "append audit event for dlq_1",
    ]
    assert published == ["dlq_1"]
    assert recovery.replayed("dlq_1") is True
