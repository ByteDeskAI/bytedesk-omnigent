"""Integration cutover checklist compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_cutover_checklist import (
    compile_integration_cutover_checklist,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_provider_cutover_checklist_from_catalog_and_verification_matrix():
    checklist = compile_integration_cutover_checklist("slack-command-center")

    assert checklist["object"] == "integration_cutover_checklist"
    assert checklist["capability_slug"] == "slack-command-center"
    assert checklist["risk_tier"] == "external_write"
    assert checklist["required_approvals"] == [
        "tenant_admin",
        "security_owner",
        "integration_owner",
    ]
    assert [phase["id"] for phase in checklist["phases"]] == [
        "catalog-freeze",
        "credential-boundary",
        "dry-run-rehearsal",
        "limited-production-window",
        "evidence-review",
        "rollback-or-scale",
    ]
    assert checklist["phases"][0]["entry_criteria"] == [
        "capability slug resolves in the integration catalog",
        "auth model and required scopes are documented",
        "business case and future unlocks are present",
    ]
    assert "communication-loop" in checklist["verification_gate_ids"]
    assert checklist["minimum_required_evidence_count"] >= 20


def test_compiles_internal_harness_cutover_with_lighter_approval_boundary():
    checklist = compile_integration_cutover_checklist("archon-style-workflow-blueprints")

    assert checklist["risk_tier"] == "internal_harness"
    assert checklist["required_approvals"] == ["integration_owner"]
    assert checklist["phases"][1]["owner"] == "workflow architect"
    assert checklist["phases"][3]["title"] == "Run deterministic workflow rehearsal"


def test_unknown_capability_cutover_checklist_returns_none():
    assert compile_integration_cutover_checklist("missing") is None


def test_integration_capability_route_exposes_cutover_checklist():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/cutover-checklist"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["phases"][-1]["id"] == "rollback-or-scale"

    missing = client.get("/v1/integration-capabilities/not-real/cutover-checklist")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
