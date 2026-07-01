"""Integration risk register compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_risk_register import (
    compile_integration_risk_register,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_external_write_risk_register_with_policy_blocker():
    register = compile_integration_risk_register("slack-command-center")

    assert register["object"] == "integration_risk_register"
    assert register["capability_slug"] == "slack-command-center"
    assert register["risk_tier"] == "external_write"
    assert register["requires_policy_approval"] is True
    assert register["minimum_control_count"] == sum(
        len(risk["controls"]) for risk in register["risks"]
    )
    assert [risk["id"] for risk in register["risks"]][:3] == [
        "credential-exposure",
        "unauthorized-provider-write",
        "event-spoofing",
    ]
    policy_risk = register["risks"][1]
    assert policy_risk["severity"] == "high"
    assert policy_risk["blocked_until_evidence"] == "policy-approval"
    assert "approval" in " ".join(policy_risk["controls"]).lower()


def test_compiles_archon_internal_harness_risk_register_without_external_write_blocker():
    register = compile_integration_risk_register("archon-style-workflow-blueprints")

    assert register["risk_tier"] == "internal_harness"
    assert register["requires_policy_approval"] is False
    assert [risk["id"] for risk in register["risks"]] == [
        "workflow-drift",
        "phase-evidence-gap",
        "operator-blindness",
    ]
    assert register["risks"][0]["blocked_until_evidence"] == "workflow-determinism"


def test_unknown_capability_risk_register_returns_none():
    assert compile_integration_risk_register("missing") is None


def test_integration_capability_route_exposes_risk_register():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/github-engineering-copilot/risk-register")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["risks"][-1]["id"] == "review-bypass"

    missing = client.get("/v1/integration-capabilities/not-real/risk-register")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
