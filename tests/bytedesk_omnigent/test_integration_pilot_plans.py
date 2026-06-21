"""Integration pilot plan compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_pilot_plans import compile_integration_pilot_plan
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_archon_workflow_harness_pilot_plan_is_internal_and_deterministic():
    plan = compile_integration_pilot_plan("archon-style-workflow-blueprints")

    assert plan is not None
    payload = plan.to_dict()
    assert payload["object"] == "integration_pilot_plan"
    assert payload["capability_slug"] == "archon-style-workflow-blueprints"
    assert payload["pilot_tier"] == "internal_harness"
    assert payload["success_metrics"] == [
        "at least 3 deterministic workflow blueprint dry runs complete without manual repair",
        "100% of phase outputs include typed evidence references",
        "operator can replay one failed phase from stored inputs",
    ]
    assert payload["pilot_boundaries"][0] == "no external tenant credentials required"
    assert payload["recommended_stakeholders"] == [
        "platform engineering owner",
        "agent operations lead",
        "workflow template reviewer",
    ]


def test_external_write_pilot_plan_requires_limited_customer_sandbox():
    plan = compile_integration_pilot_plan("slack-command-center")

    assert plan is not None
    payload = plan.to_dict()
    assert payload["pilot_tier"] == "external_write"
    assert "single sandbox workspace or tenant only" in payload["pilot_boundaries"]
    assert "all outbound writes require explicit operator approval" in payload["pilot_boundaries"]
    assert "customer success pilot owner" in payload["recommended_stakeholders"]
    assert payload["exit_criteria"][-1] == "business value owner signs off on GA readiness"


def test_unknown_capability_has_no_pilot_plan():
    assert compile_integration_pilot_plan("missing") is None


def test_integration_capability_route_exposes_pilot_plan():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/slack-command-center/pilot-plan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "integration_pilot_plan"
    assert payload["capability_slug"] == "slack-command-center"


def test_integration_capability_route_404s_unknown_pilot_plan():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/not-real/pilot-plan")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
