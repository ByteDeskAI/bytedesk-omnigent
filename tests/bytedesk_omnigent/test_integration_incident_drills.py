"""Integration incident drill compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_incident_drills import (
    compile_integration_incident_drill,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_incident_drill():
    drill = compile_integration_incident_drill("archon-style-workflow-blueprints")

    assert drill["object"] == "integration_incident_drill"
    assert drill["capability_slug"] == "archon-style-workflow-blueprints"
    assert drill["capability_name"] == "Archon-style deterministic workflow blueprints"
    assert drill["risk_tier"] == "internal_harness"
    assert drill["trigger"] == {
        "id": "workflow-phase-stall",
        "title": "Workflow harness phase stalls or emits inconsistent evidence",
        "detection_signals": [
            "phase exceeds its declared timeout or retry budget",
            "declared output artifact is missing, malformed, or attached to the wrong phase id",
            "completion evidence conflicts with the workflow graph terminal state",
        ],
    }
    assert drill["containment_actions"][:2] == [
        "pause new autonomous launches for this capability slug",
        "preserve task ids, provider object ids, agent ids, and raw event fingerprints",
    ]
    assert "replay affected phases from the last verified idempotency checkpoint" in drill[
        "recovery_gates"
    ]
    assert drill["minimum_operator_roles"] == ["incident_commander", "integration_owner"]


def test_compiles_provider_write_incident_drill():
    drill = compile_integration_incident_drill("slack-command-center")

    assert drill["risk_tier"] == "external_write"
    assert drill["trigger"]["id"] == "external-write-side-effect"
    assert "disable outbound write actions while preserving read-only ingest" in drill[
        "containment_actions"
    ]
    assert "customer_contact" in drill["minimum_operator_roles"]
    assert drill["customer_update_template"] == (
        "We detected an issue in the Slack command center integration, paused risky automation, "
        "preserved execution evidence, and are validating recovery before re-enabling writes."
    )


def test_unknown_capability_incident_drill_returns_none():
    assert compile_integration_incident_drill("missing") is None


def test_integration_capability_route_exposes_incident_drill():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/google-workspace-operator/incident-drill")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["risk_tier"] == "external_read"
    assert payload["trigger"]["id"] == "external-read-staleness"
    assert "knowledge_owner" in payload["minimum_operator_roles"]

    missing = client.get("/v1/integration-capabilities/not-real/incident-drill")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
