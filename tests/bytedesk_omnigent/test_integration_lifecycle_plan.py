"""Integration capability lifecycle plan tests."""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_lifecycle_plan import (
    compile_integration_lifecycle_plan,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_lifecycle_without_external_auth_stage() -> None:
    plan = compile_integration_lifecycle_plan("archon-style-workflow-blueprints")

    assert plan is not None
    assert plan["object"] == "integration_capability_lifecycle_plan"
    assert plan["capability_slug"] == "archon-style-workflow-blueprints"
    assert plan["risk_tier"] == "internal_harness"
    stages = cast(list[dict[str, object]], plan["stages"])
    assert [stage["id"] for stage in stages] == [
        "catalog-selected",
        "blueprint-bound",
        "sandbox-validated",
        "pilot-enabled",
        "production-active",
        "suspended",
        "retired",
    ]
    assert all("oauth" not in str(stage["id"]) for stage in stages)
    assert plan["terminal_states"] == ["production-active", "suspended", "retired"]
    assert cast(int, plan["minimum_evidence_count"]) >= 14


def test_compiles_external_write_lifecycle_with_auth_and_policy_gates() -> None:
    plan = compile_integration_lifecycle_plan("slack-command-center")

    assert plan is not None
    assert plan["risk_tier"] == "external_write"
    stages = cast(list[dict[str, object]], plan["stages"])
    assert [stage["id"] for stage in stages][:3] == [
        "catalog-selected",
        "oauth-authorized",
        "webhook-bound",
    ]
    policy_stage = next(stage for stage in stages if stage["id"] == "policy-approved")
    policy_evidence = cast(list[str], policy_stage["required_evidence"])
    assert "write actions are mapped to approval policy" in policy_evidence
    assert "channels:history" in cast(list[str], plan["required_scopes"])
    transitions = cast(dict[str, list[str]], plan["allowed_transitions"])
    assert transitions["policy-approved"] == ["pilot-enabled"]


def test_lifecycle_plan_unknown_slug_returns_none() -> None:
    assert compile_integration_lifecycle_plan("missing") is None


def test_integration_capability_route_exposes_lifecycle_plan() -> None:
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/slack-command-center/lifecycle-plan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "integration_capability_lifecycle_plan"
    assert payload["capability_name"] == "Slack command center"
    assert payload["allowed_transitions"]["pilot-enabled"] == ["production-active", "suspended"]

    missing = client.get("/v1/integration-capabilities/not-real/lifecycle-plan")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
