"""Route tests for deterministic integration workflow harness."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.integration_harness import create_integration_harness_router


def test_integration_harness_route_returns_compiled_contract() -> None:
    app = FastAPI()
    app.include_router(create_integration_harness_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-workflow-harness",
        params={
            "provider": "slack",
            "objective": "triage support escalations",
            "agent_id": "support-orchestrator",
            "external_object": "channel:#vip-support",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "slack"
    assert body["idempotency_key"].startswith("integration-harness:slack:")
    assert body["phases"][0]["id"] == "intake"
    assert body["phases"][-1]["id"] == "handoff"
