"""Integration SLO profile compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_slo_profiles import compile_integration_slo_profile
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_internal_workflow_harness_slo_profile():
    profile = compile_integration_slo_profile("archon-style-workflow-blueprints")

    assert profile is not None
    assert profile["object"] == "integration_slo_profile"
    assert profile["capability_slug"] == "archon-style-workflow-blueprints"
    assert profile["risk_tier"] == "internal_harness"
    assert profile["availability_target"] == "99.0% monthly"
    assert profile["sync_freshness_target"] == "phase state updates visible within 30 seconds"
    assert profile["error_budget_policy"]["freeze_threshold"] == "25% of monthly budget remaining"
    assert profile["measurement_events"] == [
        "workflow.phase.started",
        "workflow.phase.completed",
        "workflow.phase.failed",
    ]
    assert profile["operator_promises"][0].startswith("Workflow phases")


def test_compiles_external_write_provider_slo_profile_with_category_controls():
    profile = compile_integration_slo_profile("slack-command-center")

    assert profile is not None
    assert profile["risk_tier"] == "external_write"
    assert profile["availability_target"] == "99.5% monthly"
    assert profile["sync_freshness_target"] == "provider events normalized within 2 minutes"
    assert profile["action_latency_target"] == "95% of approved writes complete within 60 seconds"
    assert profile["measurement_events"] == [
        "integration.event.received",
        "integration.event.normalized",
        "integration.action.approved",
        "integration.action.completed",
        "integration.action.failed",
    ]
    assert "Outbound collaboration messages are rate-limited and auditable." in profile[
        "category_controls"
    ]


def test_unknown_capability_slo_profile_returns_none():
    assert compile_integration_slo_profile("missing") is None


def test_integration_capability_route_exposes_slo_profile():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/github-engineering-copilot/slo-profile")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert "Pull request and CI automation keeps review-safe evidence." in payload[
        "category_controls"
    ]

    missing = client.get("/v1/integration-capabilities/not-real/slo-profile")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
