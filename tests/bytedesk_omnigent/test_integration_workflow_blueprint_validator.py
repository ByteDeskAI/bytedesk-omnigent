"""Integration workflow blueprint validation tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_workflow_blueprint_validator import (
    validate_integration_workflow_blueprint,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_validates_deterministic_archon_style_blueprint():
    report = validate_integration_workflow_blueprint(
        {
            "capability_slug": "archon-style-workflow-blueprints",
            "phases": [
                {
                    "id": "discover-context",
                    "role": "planner",
                    "inputs": ["catalog capability", "tenant objective"],
                    "outputs": ["implementation plan"],
                    "completion_evidence": ["plan accepted by operator"],
                },
                {
                    "id": "verify-output",
                    "role": "qa-agent",
                    "depends_on": ["discover-context"],
                    "inputs": ["implementation plan"],
                    "outputs": ["verification report"],
                    "completion_evidence": ["targeted tests pass"],
                },
            ],
        }
    )

    assert report["object"] == "integration_workflow_blueprint_validation"
    assert report["capability_slug"] == "archon-style-workflow-blueprints"
    assert report["valid"] is True
    assert report["phase_count"] == 2
    assert report["deterministic_node_ids"] == ["discover-context", "verify-output"]
    assert report["issues"] == []


def test_reports_duplicate_missing_and_cyclic_phase_errors():
    report = validate_integration_workflow_blueprint(
        {
            "capability_slug": "slack-command-center",
            "phases": [
                {
                    "id": "triage",
                    "role": "intake-agent",
                    "depends_on": ["publish"],
                    "inputs": ["slack event"],
                    "outputs": ["task brief"],
                    "completion_evidence": ["task id recorded"],
                },
                {
                    "id": "publish",
                    "role": "collaboration-agent",
                    "depends_on": ["triage", "missing-node"],
                    "inputs": ["task brief"],
                    "outputs": ["thread reply"],
                    "completion_evidence": [],
                },
                {
                    "id": "publish",
                    "role": "audit-agent",
                    "inputs": [],
                    "outputs": ["audit note"],
                    "completion_evidence": ["audit note stored"],
                },
            ],
        }
    )

    assert report["valid"] is False
    issue_codes = {issue["code"] for issue in report["issues"]}
    assert {
        "duplicate_phase_id",
        "missing_dependency",
        "cycle_detected",
        "missing_completion_evidence",
        "missing_inputs",
    } <= issue_codes


def test_integration_capability_route_validates_workflow_blueprints():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-capabilities/workflow-blueprints/validate",
        json={
            "capability_slug": "github-engineering-copilot",
            "phases": [
                {
                    "id": "ingest-issue",
                    "role": "developer-agent",
                    "inputs": ["GitHub issue webhook"],
                    "outputs": ["Omnigent task"],
                    "completion_evidence": ["task id linked to issue"],
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["capability_slug"] == "github-engineering-copilot"

    invalid = client.post(
        "/v1/integration-capabilities/workflow-blueprints/validate",
        json={"capability_slug": "not-real", "phases": []},
    )
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False
    assert invalid.json()["issues"][0]["code"] == "unknown_capability"
