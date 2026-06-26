from __future__ import annotations

from omnigent.fabric.manifest import DEFAULT_FABRIC_MANIFEST
from omnigent.fabric.models import (
    CredentialReference,
    FabricEnvelope,
    RunnerJob,
    SchedulerJob,
    TimelineEvent,
    fabric_schema_hash,
)


def test_fabric_envelope_round_trips_with_canonical_schema_hash() -> None:
    job = RunnerJob(
        job_id="job_1",
        session_id="conv_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        epoch=7,
        deadline_unix_ms=1_800_000,
        capacity={"cpu": 1, "memory_mb": 512},
        credential=CredentialReference(
            ref="cred_1",
            principal_id="agent_1",
            expires_unix_ms=1_900_000,
        ),
    )
    envelope = FabricEnvelope.wrap(
        subject="omnigent.runner.jobs.default",
        idempotency_key="runner:conv_1:7",
        payload=job,
    )

    encoded = envelope.to_json()
    decoded = FabricEnvelope.from_json(encoded, RunnerJob)

    assert decoded == envelope
    assert decoded.schema_hash == fabric_schema_hash(RunnerJob)
    assert decoded.payload.credential.ref == "cred_1"


def test_scheduler_job_uses_claim_check_payload_shape() -> None:
    job = SchedulerJob(
        job_id="sched_1",
        schedule_id="schedule_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1_800_000,
        idempotency_key="schedule:schedule_1:1800000",
        payload_ref="sql-outbox:42",
    )

    assert job.to_dict()["payload_ref"] == "sql-outbox:42"
    assert job.to_dict()["idempotency_key"] == "schedule:schedule_1:1800000"


def test_timeline_event_is_session_visible_canonical_data_model() -> None:
    event = TimelineEvent(
        event_id="evt_1",
        session_id="conv_1",
        stage="cold-spawn",
        message="runner cold spawn requested",
        occurred_unix_ms=1_800_000,
    )

    assert event.to_dict() == {
        "event_id": "evt_1",
        "session_id": "conv_1",
        "stage": "cold-spawn",
        "message": "runner cold spawn requested",
        "occurred_unix_ms": 1_800_000,
        "metadata": {},
    }


def test_manifest_declares_required_assets_and_non_overlapping_workqueue_subjects() -> None:
    manifest = DEFAULT_FABRIC_MANIFEST

    assert {stream.name for stream in manifest.streams} == {
        "OMNIGENT_SCHEDULER_JOBS",
        "OMNIGENT_RUNNER_JOBS",
        "OMNIGENT_RUNNER_EVENTS",
        "OMNIGENT_RUNNER_DLQ",
        "OMNIGENT_FABRIC_AUDIT",
    }
    assert "omnigent-fabric-runner-registry" in {bucket.name for bucket in manifest.kv_buckets}
    assert "omnigent-fabric-artifact-manifests" in {
        store.name for store in manifest.object_stores
    }

    runner_subjects = [
        subject
        for stream in manifest.streams
        if stream.name == "OMNIGENT_RUNNER_JOBS"
        for subject in stream.subjects
    ]
    assert runner_subjects == [
        "omnigent.runner.jobs.default",
        "omnigent.runner.jobs.priority",
        "omnigent.runner.jobs.maintenance",
    ]
    assert manifest.lane_subject("default") == "omnigent.runner.jobs.default"
    assert manifest.lane_subject("priority") == "omnigent.runner.jobs.priority"
    assert manifest.lane_subject("default") != manifest.lane_subject("priority")
