"""Integration access-control plan compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_access_plan import compile_integration_access_plan
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_external_write_access_plan_for_slack():
    plan = compile_integration_access_plan("slack-command-center")

    assert plan is not None
    assert plan["object"] == "integration_access_plan"
    assert plan["capability_slug"] == "slack-command-center"
    assert plan["risk_tier"] == "external_write"
    assert plan["least_privilege_roles"] == [
        {
            "id": "integration_viewer",
            "title": "Integration viewer",
            "allowed_actions": [
                "read_catalog",
                "read_provider_objects",
                "read_execution_evidence",
            ],
        },
        {
            "id": "integration_operator",
            "title": "Integration operator",
            "allowed_actions": [
                "trigger_read_only_sync",
                "draft_provider_write",
                "request_write_approval",
            ],
        },
        {
            "id": "integration_approver",
            "title": "Integration approver",
            "allowed_actions": ["approve_provider_write", "disable_connector"],
        },
    ]
    assert plan["approval_required_for"] == [
        "provider-side writes",
        "message or comment publication",
        "connector disablement",
    ]
    assert plan["blocked_without_approval"] == [
        "write external object",
        "publish outbound communication",
        "delete or revoke provider resource",
    ]
    assert "chat:write" in plan["scope_review"]["write_scopes"]


def test_compiles_internal_harness_access_plan_without_external_scope_review():
    plan = compile_integration_access_plan("archon-style-workflow-blueprints")

    assert plan is not None
    assert plan["risk_tier"] == "internal_harness"
    assert plan["approval_required_for"] == [
        "template publication",
        "cross-agent workflow activation",
    ]
    assert plan["scope_review"] == {
        "read_scopes": [],
        "write_scopes": [],
        "offline_scopes": [],
    }
    assert plan["least_privilege_roles"][-1]["id"] == "workflow_publisher"


def test_unknown_integration_access_plan_returns_none():
    assert compile_integration_access_plan("not-real") is None


def test_integration_capability_route_exposes_access_plan():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/github-engineering-copilot/access-plan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["risk_tier"] == "external_write"
    assert payload["least_privilege_roles"][-1]["id"] == "integration_approver"

    missing = client.get("/v1/integration-capabilities/not-real/access-plan")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
