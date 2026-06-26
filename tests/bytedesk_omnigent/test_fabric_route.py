from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.fabric.extension import BytedeskFabricExtension
from bytedesk_omnigent.fabric.outbox import (
    SqlAlchemyFabricOutboxStore,
    SqlOutboxSchedulerDispatch,
)
from bytedesk_omnigent.routes.fabric import create_fabric_router
from omnigent.fabric.models import SchedulerJob
from omnigent.fabric.policies import (
    InMemoryFabricCapacityPolicy,
    InMemoryQuarantinePolicy,
)
from omnigent.fabric.preflight import (
    FabricCheck,
    FabricPreflightReport,
    InMemoryFabricInspector,
    build_preflight_report,
    fabric_capabilities,
)
from omnigent.kernel.extensions import OmnigentExtension
from omnigent.sdk.contrib import CONTRIB_ATTR


def _store_uri(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'fabric_route.db'}"


def _scheduler_job(
    *,
    job_id: str = "sched_1",
    schedule_id: str = "cron_1",
    idempotency_key: str = "schedule:cron_1:1",
) -> SchedulerJob:
    return SchedulerJob(
        job_id=job_id,
        schedule_id=schedule_id,
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1_000,
        idempotency_key=idempotency_key,
        payload_ref=f"sql:fabric_outbox:{idempotency_key}",
    )


def test_fabric_extension_uses_omnigent_sdk() -> None:
    ext = BytedeskFabricExtension()

    assert isinstance(ext, OmnigentExtension)
    assert ext.name == "bytedesk.fabric"
    assert len(ext.routers()) == 1
    assert getattr(BytedeskFabricExtension.fabric_router, CONTRIB_ATTR)["seam"] == "router"
    assert getattr(BytedeskFabricExtension.fabric_service_background, CONTRIB_ATTR)[
        "seam"
    ] == "background"


def test_fabric_preflight_route_exposes_pass_fail_detail() -> None:
    inspector = InMemoryFabricInspector(
        report=FabricPreflightReport(
            status="fail",
            checks=(
                FabricCheck(
                    name="nats",
                    status="fail",
                    detail="OMNIGENT_NATS_URL is not configured",
                ),
                FabricCheck(name="legacy_absence", status="pass"),
            ),
            services={"omnigent.fabric.control": "missing"},
            schema_hashes={"runner_job": "abc"},
        )
    )
    app = FastAPI()
    app.include_router(create_fabric_router(inspector=inspector), prefix="/v1")

    response = TestClient(app).get("/v1/fabric/preflight")

    assert response.status_code == 503
    assert response.json()["status"] == "fail"
    assert response.json()["checks"][0]["name"] == "nats"
    assert response.json()["checks"][0]["status"] == "fail"
    assert response.json()["legacy_absence"]["runner_ws_transport"] is True
    assert response.json()["legacy_absence"]["direct_host_launch_fallback"] is True


def test_fabric_routes_return_manifest_projection() -> None:
    app = FastAPI()
    app.include_router(
        create_fabric_router(inspector=InMemoryFabricInspector()),
        prefix="/v1",
    )
    client = TestClient(app)

    lanes = client.get("/v1/fabric/lanes")
    topology = client.get("/v1/fabric/topology")

    assert lanes.status_code == 200
    assert lanes.json()["data"][0]["subject"] == "omnigent.runner.jobs.default"
    assert topology.status_code == 200
    assert "OMNIGENT_RUNNER_JOBS" in topology.json()["streams"]


def test_fabric_outbox_route_lists_sql_summaries(tmp_path) -> None:
    store = SqlAlchemyFabricOutboxStore(_store_uri(tmp_path))
    record, _ = SqlOutboxSchedulerDispatch(store).enqueue_job(_scheduler_job())
    app = FastAPI()
    app.include_router(create_fabric_router(outbox_store=store), prefix="/v1")

    response = TestClient(app).get("/v1/fabric/outbox")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "fabric_outbox.list"
    assert len(body["data"]) == 1
    summary = body["data"][0]
    assert summary["id"] == record.id
    assert summary["idempotency_key"] == "schedule:cron_1:1"
    assert summary["status"] == "pending"
    assert summary["metadata"]["schedule_id"] == "cron_1"
    assert "payload" not in summary


def test_fabric_outbox_route_filters_by_status(tmp_path) -> None:
    store = SqlAlchemyFabricOutboxStore(_store_uri(tmp_path))
    record, _ = SqlOutboxSchedulerDispatch(store).enqueue_job(_scheduler_job())
    store.mark_published(record.id, now=42)
    app = FastAPI()
    app.include_router(create_fabric_router(outbox_store=store), prefix="/v1")
    client = TestClient(app)

    pending = client.get("/v1/fabric/outbox?status=pending")
    published = client.get("/v1/fabric/outbox?status=published")

    assert pending.status_code == 200
    assert pending.json()["data"] == []
    assert published.status_code == 200
    assert [item["id"] for item in published.json()["data"]] == [record.id]


def test_fabric_capacity_route_lists_policy_records() -> None:
    capacity = InMemoryFabricCapacityPolicy(now_ms=lambda: 1_000)
    capacity.open_circuit(scope="tenant", key="tenant_1", reason="ops")
    app = FastAPI()
    app.include_router(create_fabric_router(capacity_policy=capacity), prefix="/v1")

    response = TestClient(app).get("/v1/fabric/capacity")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "fabric_capacity.list"
    assert body["data"] == [
        {
            "scope": "tenant",
            "key": "tenant_1",
            "limit": 10,
            "used": 0,
            "updated_unix_ms": 1_000,
            "circuit_open": True,
            "metadata": {"reason": "ops"},
        }
    ]


def test_fabric_quarantine_route_lists_policy_records() -> None:
    quarantine = InMemoryQuarantinePolicy(now_ms=lambda: 1_000)
    quarantine.apply(
        resource_type="runner",
        resource_id="runner_1",
        reason="crash",
        metadata={"host_id": "host_1"},
    )
    app = FastAPI()
    app.include_router(create_fabric_router(quarantine_policy=quarantine), prefix="/v1")

    response = TestClient(app).get("/v1/fabric/quarantine")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "fabric_quarantine.list"
    assert body["data"] == [
        {
            "resource_type": "runner",
            "resource_id": "runner_1",
            "reason": "crash",
            "failures": 0,
            "quarantined_unix_ms": 1_000,
            "metadata": {"host_id": "host_1"},
        }
    ]


def test_fabric_preflight_uses_registered_required_services(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://127.0.0.1:4222")

    report = build_preflight_report()

    assert report.status == "pass"
    assert report.services["omnigent.fabric.control"] == "1.0.0"
    assert report.services["omnigent.fabric.outbox"] == "1.0.0"
    checks = {check.name: check for check in report.checks}
    assert checks["services"].status == "pass"

    capabilities = fabric_capabilities()
    assert capabilities["active"] is True
    assert capabilities["nats_ready"] is True
    assert capabilities["service_versions"]["omnigent.fabric.scheduler"] == "1.0.0"
