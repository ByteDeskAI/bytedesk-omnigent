"""Tests for deterministic third-party integration rollback plans.

The compiler gives ByteDesk Platform a safe, auditable compensation contract before
an autonomous agent mutates an external SaaS object.  It is deliberately pure: no
secrets, no network calls, and stable IDs so platform UI / approvals can preview
the same plan the agent later executes.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.extension import BytedeskExtension
from bytedesk_omnigent.integration_rollback import compile_integration_rollback_plan


def test_compile_integration_rollback_plan_is_deterministic_and_provider_aware() -> None:
    plan = compile_integration_rollback_plan(
        provider="Jira",
        operation="transition_issue",
        agent_id="agent-release-manager",
        external_ref="PROJ-123",
        mutation_summary="Move customer incident to Done after remediation",
        risk_level="high",
    )
    again = compile_integration_rollback_plan(
        provider="jira",
        operation="transition_issue",
        agent_id="agent-release-manager",
        external_ref="PROJ-123",
        mutation_summary="Move customer incident to Done after remediation",
        risk_level="high",
    )

    assert plan == again
    assert plan.provider == "jira"
    assert plan.plan_id.startswith("rollback:jira:")
    assert plan.idempotency_key.startswith("integration-rollback:jira:")
    assert plan.requires_approval is True
    assert plan.steps[0].name == "capture_pre_mutation_snapshot"
    assert plan.steps[-1].name == "publish_handoff_receipt"
    assert "previous_status" in plan.required_snapshot_fields
    assert "restored_status" in plan.verification_evidence


def test_unknown_provider_gets_generic_safe_rollback_contract() -> None:
    plan = compile_integration_rollback_plan(
        provider="custom-crm",
        operation="update_contact",
        agent_id="agent-crm",
        external_ref="contact-9",
    )

    assert plan.provider == "custom-crm"
    assert plan.requires_approval is True
    assert plan.required_snapshot_fields == ("external_ref", "before_state", "changed_fields")
    assert plan.verification_evidence == (
        "external_ref",
        "post_rollback_state",
        "operator_receipt",
    )
    assert [step.name for step in plan.steps] == [
        "capture_pre_mutation_snapshot",
        "freeze_followup_automation",
        "apply_compensation",
        "verify_external_state",
        "publish_handoff_receipt",
    ]


def test_extension_exposes_integration_rollback_plan_route() -> None:
    app = FastAPI()
    for router in BytedeskExtension().routers():
        app.include_router(router, prefix="/v1")

    response = TestClient(app).get(
        "/v1/integration-rollback-plan",
        params={
            "provider": "github",
            "operation": "close_issue",
            "agent_id": "agent-support",
            "external_ref": "ByteDeskAI/bytedesk-omnigent#123",
            "risk_level": "medium",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "github"
    assert payload["operation"] == "close-issue"
    assert payload["external_ref"] == "ByteDeskAI/bytedesk-omnigent#123"
    assert payload["requires_approval"] is True
    assert payload["steps"][0]["gate"] == "snapshot_recorded"
