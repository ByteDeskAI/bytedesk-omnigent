"""Integration deprecation plan compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_deprecation_plan import (
    compile_integration_deprecation_plan,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_external_write_deprecation_plan_for_slack():
    plan = compile_integration_deprecation_plan("slack-command-center")

    assert plan["object"] == "integration_deprecation_plan"
    assert plan["capability_slug"] == "slack-command-center"
    assert plan["risk_tier"] == "external_write"
    assert plan["requires_customer_notice"] is True
    assert plan["minimum_notice_days"] == 14
    assert [phase["id"] for phase in plan["phases"]] == [
        "announce-freeze",
        "drain-ingress",
        "disable-mutations",
        "archive-evidence",
        "revoke-credentials",
        "finalize-successor",
    ]
    assert "channel export or thread permalink inventory" in plan["category_retention_notes"]
    assert plan["reversible_until_phase"] == "revoke-credentials"


def test_compiles_internal_workflow_harness_deprecation_plan():
    plan = compile_integration_deprecation_plan("archon-style-workflow-blueprints")

    assert plan["risk_tier"] == "internal_harness"
    assert plan["requires_customer_notice"] is False
    assert plan["minimum_notice_days"] == 0
    assert plan["phases"][0]["owner"] == "platform-operator"
    assert "workflow run graph snapshots" in plan["category_retention_notes"]
    assert plan["successor_requirements"] == [
        "successor owner or replacement workflow is named",
        "remaining tasks have an explicit migration or cancellation outcome",
        "operators can still read historical execution evidence",
    ]


def test_unknown_capability_deprecation_plan_returns_none():
    assert compile_integration_deprecation_plan("missing") is None


def test_integration_capability_route_exposes_deprecation_plan():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/deprecation-plan"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["phases"][2]["id"] == "disable-mutations"

    missing = client.get("/v1/integration-capabilities/not-real/deprecation-plan")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
