"""Tests for deterministic connected-app approval plans."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_approval import compile_integration_approval_plan
from bytedesk_omnigent.routes.integration_approval import create_integration_approval_router


def test_readonly_slack_plan_needs_no_human_gate() -> None:
    plan = compile_integration_approval_plan(
        provider="Slack",
        scopes=["channels:history", "users:read"],
        requested_operations=["summarize_channel"],
    )

    assert plan.provider == "slack"
    assert plan.risk_level == "low"
    assert plan.required_approval == "none"
    assert plan.readonly_scopes == ["channels:history", "users:read"]
    assert plan.write_scopes == []
    assert plan.admin_scopes == []
    assert plan.gates == ["scope_preview", "audit_log_entry"]
    assert plan.idempotency_key == (
        "integration-approval:slack:channels:history,users:read:summarize_channel"
    )


def test_google_workspace_writeback_with_admin_scope_requires_two_key() -> None:
    plan = compile_integration_approval_plan(
        provider="google-workspace",
        scopes=["https://www.googleapis.com/auth/admin.directory.user", "gmail.send"],
        requested_operations=["send_follow_up_email"],
        writeback_enabled=True,
    )

    assert plan.provider == "google_workspace"
    assert plan.risk_level == "critical"
    assert plan.required_approval == "two_key"
    assert "workspace_admin_approval" in plan.gates
    assert "second_reviewer_approval" in plan.gates
    assert "dry_run_before_writeback" in plan.gates
    assert "admin_or_workspace_scopes" in plan.reasons
    assert "autonomous_writeback_requested" in plan.reasons
    assert plan.recommended_token_owner == "workspace_admin"


def test_system_of_record_writeback_requires_admin_approval() -> None:
    plan = compile_integration_approval_plan(
        provider="HubSpot",
        scopes=["crm.objects.contacts.write", "crm.objects.contacts.read"],
        requested_operations=["update_contact_after_agent_resolution"],
    )

    assert plan.provider == "hubspot"
    assert plan.risk_level == "high"
    assert plan.required_approval == "admin"
    assert plan.write_scopes == ["crm.objects.contacts.write"]
    assert "system_of_record_provider" in plan.reasons


def test_compile_route_returns_approval_plan() -> None:
    app = FastAPI()
    app.include_router(create_integration_approval_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-approval-plans/compile",
        json={
            "provider": "slack",
            "scopes": ["channels:history", "chat:write"],
            "requested_operations": ["notify_channel"],
        },
    )

    assert response.status_code == 200
    body = response.json()["approval_plan"]
    assert body["provider"] == "slack"
    assert body["required_approval"] == "user"
    assert body["write_scopes"] == ["chat:write"]
    assert body["byte_desk_mount_hint"] == "/integrations/slack/approval-preview"


def test_compile_route_rejects_blank_provider() -> None:
    app = FastAPI()
    app.include_router(create_integration_approval_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-approval-plans/compile",
        json={"provider": "", "scopes": ["users:read"]},
    )

    assert response.status_code == 400
    assert response.json()["status"] == "invalid_request"
