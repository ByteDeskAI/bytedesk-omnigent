"""Tests for deterministic integration replay plans (loop iteration 18)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.extension import BytedeskExtension
from bytedesk_omnigent.integration_replay_plans import compile_integration_replay_plan
from omnigent.extensions import install_extensions


def test_compile_replay_plan_classifies_customer_writeback_as_manual_review() -> None:
    plan = compile_integration_replay_plan(
        {
            "provider": "HubSpot",
            "workspace_id": "ws_123",
            "event_type": "contact.propertyChange",
            "operation": "update_crm_record",
            "external_id": "contact_42",
            "writeback": True,
        }
    )

    assert plan["provider"] == "hubspot"
    assert plan["replay_strategy"] == "dedupe_then_manual_review"
    assert plan["risk_level"] == "high"
    assert plan["requires_approval"] is True
    assert plan["idempotency_key"] == (
        "integration-replay:v1:hubspot:ws_123:contact.propertyChange:contact_42"
    )
    assert plan["dead_letter"]["recommended_queue"] == "integration.hubspot.dead_letter"
    assert [step["id"] for step in plan["steps"]] == [
        "normalize_event",
        "dedupe_event",
        "verify_binding",
        "approval_gate",
        "dispatch_agent",
        "record_receipt",
        "writeback",
    ]


def test_compile_replay_plan_makes_collaboration_mentions_fast_path() -> None:
    plan = compile_integration_replay_plan(
        {
            "provider": "Discord",
            "workspace_id": "guild_abc",
            "event_type": "message.create",
            "operation": "agent_mention_triage",
            "external_id": "msg_9",
        }
    )

    assert plan["provider"] == "discord"
    assert plan["replay_strategy"] == "dedupe_then_dispatch"
    assert plan["risk_level"] == "low"
    assert plan["requires_approval"] is False
    assert "approval_gate" not in [step["id"] for step in plan["steps"]]
    assert plan["retry_policy"] == {
        "max_attempts": 3,
        "backoff": "exponential",
        "base_delay_seconds": 30,
    }


def test_compile_replay_plan_route_is_mounted_by_bytedesk_extension() -> None:
    app = FastAPI()
    install_extensions(app, extensions=[BytedeskExtension()])
    resp = TestClient(app).post(
        "/v1/integration-replay-plans/compile",
        json={
            "provider": "GitHub",
            "workspace_id": "org/repo",
            "event_type": "issues",
            "operation": "create_work_item",
            "external_id": "issue_17",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "github"
    assert body["idempotency_key"] == (
        "integration-replay:v1:github:org/repo:issues:issue_17"
    )


def test_compile_replay_plan_rejects_missing_required_fields() -> None:
    app = FastAPI()
    install_extensions(app, extensions=[BytedeskExtension()])
    resp = TestClient(app).post(
        "/v1/integration-replay-plans/compile",
        json={"provider": "Slack", "workspace_id": "T123"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing required fields: event_type, operation"
