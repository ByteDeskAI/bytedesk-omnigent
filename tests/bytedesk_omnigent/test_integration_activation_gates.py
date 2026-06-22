"""Tests for deterministic connected-app activation gates."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_activation_gates import (
    compile_integration_activation_gate,
)
from bytedesk_omnigent.routes.integration_activation_gates import (
    create_integration_activation_gates_router,
)


def test_activation_gate_blocks_until_platform_artifacts_are_ready() -> None:
    """A writeback-capable Slack app cannot be enabled until every deterministic
    safety artifact has passed, including the agent handoff package."""

    plan = compile_integration_activation_gate(
        provider="Slack",
        workspace_id="acme",
        connected_app_id="app_123",
        capabilities=["webhook", "oauth", "writeback", "agent_handoff"],
        checks={
            "secret_ready": True,
            "oauth_ready": True,
            "webhook_preview_passed": False,
            "route_configured": True,
            "replay_plan_ready": True,
            "approval_policy_ready": False,
            "agent_handoff_ready": True,
        },
    )

    assert plan["activation_id"] == "integration-activation:v1:slack:acme:app_123"
    assert plan["provider"] == "slack"
    assert plan["status"] == "blocked"
    assert plan["can_enable"] is False
    assert plan["blockers"] == [
        {
            "gate": "webhook_preview_passed",
            "reason": "run a signed webhook preview before live delivery",
        },
        {
            "gate": "approval_policy_ready",
            "reason": "attach a human approval policy before provider writeback",
        },
    ]
    assert plan["next_action"] == "run a signed webhook preview before live delivery"
    assert plan["workflow_steps"] == [
        "normalize connected-app context",
        "verify secret and OAuth readiness",
        "prove webhook routing with a no-side-effect preview",
        "compile replay and approval safety contracts",
        "verify agent handoff package readiness",
        "enable live delivery in ByteDesk Platform",
    ]


def test_activation_gate_marks_non_writeback_webhook_ready() -> None:
    """A read-only Discord webhook route can activate without writeback gates."""

    plan = compile_integration_activation_gate(
        provider="Discord",
        workspace_id="helms",
        connected_app_id="discord-main",
        capabilities=["webhook", "agent_handoff"],
        checks={
            "secret_ready": True,
            "webhook_preview_passed": True,
            "route_configured": True,
            "agent_handoff_ready": True,
        },
    )

    assert plan["status"] == "ready"
    assert plan["can_enable"] is True
    assert plan["blockers"] == []
    assert plan["required_gates"] == [
        "secret_ready",
        "webhook_preview_passed",
        "route_configured",
        "agent_handoff_ready",
    ]


def test_activation_gate_route_exposes_compiler() -> None:
    app = FastAPI()
    app.include_router(create_integration_activation_gates_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-activation-gates/compile",
        json={
            "provider": "HubSpot",
            "workspace_id": "acme",
            "connected_app_id": "hs-prod",
            "capabilities": ["oauth", "writeback", "agent_handoff"],
            "checks": {
                "oauth_ready": True,
                "replay_plan_ready": True,
                "approval_policy_ready": True,
                "agent_handoff_ready": True,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["activation_id"] == "integration-activation:v1:hubspot:acme:hs-prod"
    assert body["status"] == "ready"
    assert body["required_gates"] == [
        "oauth_ready",
        "replay_plan_ready",
        "approval_policy_ready",
        "agent_handoff_ready",
    ]
