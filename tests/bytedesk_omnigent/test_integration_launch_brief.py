"""Integration launch brief compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_launch_brief import compile_integration_launch_brief
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_launch_brief_sequences_archon_harness_without_external_auth():
    brief = compile_integration_launch_brief("archon-style-workflow-blueprints")

    assert brief is not None
    assert brief["object"] == "integration_launch_brief"
    assert brief["capability_slug"] == "archon-style-workflow-blueprints"
    assert brief["risk_tier"] == "internal_harness"
    assert brief["recommended_launch_mode"] == "internal_deterministic_harness"
    assert brief["authorization_plan"]["credential_posture"] == "no_external_credentials"
    assert [phase["id"] for phase in brief["phases"]] == [
        "contract",
        "harness_dry_run",
        "operator_review",
        "production_enablement",
    ]
    assert brief["phases"][1]["required_gates"] == [
        "workflow-determinism",
        "idempotency-replay",
        "observability-evidence",
    ]
    assert brief["default_success_metric"] == (
        "100% of workflow phases emit terminal evidence in dry-run fixtures"
    )


def test_launch_brief_risk_tiers_external_write_capabilities():
    brief = compile_integration_launch_brief("slack-command-center")

    assert brief is not None
    assert brief["risk_tier"] == "external_write"
    assert brief["recommended_launch_mode"] == "approved_pilot_then_workspace_rollout"
    assert brief["authorization_plan"] == {
        "auth_model": "OAuth 2.0 bot + user tokens",
        "credential_posture": "secret_manager_required",
        "scope_review_required": True,
        "required_scopes": ["channels:history", "chat:write", "commands", "users:read"],
    }
    assert [phase["id"] for phase in brief["phases"]] == [
        "contract",
        "oauth_sandbox",
        "read_only_pilot",
        "approved_write_pilot",
        "production_enablement",
    ]
    write_phase = brief["phases"][3]
    assert write_phase["required_gates"] == ["policy-approval", "communication-loop"]
    assert write_phase["exit_criteria"] == [
        "mutating provider actions require approval and leave outcome records",
        "denied approvals produce no provider-side mutation",
    ]


def test_unknown_launch_brief_returns_none():
    assert compile_integration_launch_brief("missing") is None


def test_integration_capability_route_exposes_launch_brief():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/notion-knowledge-operator/launch-brief")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "notion-knowledge-operator"
    assert payload["risk_tier"] == "external_write"
    assert payload["recommended_launch_mode"] == "approved_pilot_then_workspace_rollout"
    assert payload["phases"][2]["id"] == "read_only_pilot"
    assert payload["phases"][3]["id"] == "approved_write_pilot"

    missing = client.get("/v1/integration-capabilities/not-real/launch-brief")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
