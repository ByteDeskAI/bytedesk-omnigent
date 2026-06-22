"""Tests for deterministic integration event route compilation."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_event_routes import compile_event_route
from bytedesk_omnigent.routes.integration_event_routes import (
    create_integration_event_routes_router,
)


def test_compile_event_route_maps_github_issue_to_capability_plan() -> None:
    plan = compile_event_route(
        provider="GitHub",
        event_type="issues.opened",
        subject_id="ByteDeskAI/bytedesk-omnigent#123",
        workspace_id="acme",
        desired_outcome="triage the issue and propose an implementation plan",
        writeback=True,
    )

    assert plan.provider == "github"
    assert plan.ingress_source == "github"
    assert plan.match_key == "issues.opened"
    assert plan.required_capability == "developer.work_item"
    assert plan.task_kind == "external.github.issue"
    assert plan.idempotency_key == (
        "integration-route:github:acme:issues.opened:"
        "ByteDeskAI/bytedesk-omnigent#123"
    )
    assert plan.approval_required is True
    assert plan.writeback_policy == "requires_approval"
    assert [step["id"] for step in plan.steps] == [
        "verify_connected_app",
        "normalize_event",
        "resolve_specialist",
        "create_or_resume_task",
        "approval_gate",
        "writeback_outcome",
    ]


def test_compile_event_route_has_deterministic_fallback_for_unknown_provider() -> None:
    plan = compile_event_route(
        provider="Custom CRM",
        event_type="contact.updated",
        subject_id="contact-42",
        workspace_id=None,
        desired_outcome=None,
        writeback=False,
    )

    assert plan.provider == "custom-crm"
    assert plan.ingress_source == "custom-crm"
    assert plan.match_key == "contact.updated"
    assert plan.required_capability == "integration.generic_event"
    assert plan.task_kind == "external.custom-crm.event"
    assert plan.idempotency_key == "integration-route:custom-crm:global:contact.updated:contact-42"
    assert plan.approval_required is False
    assert plan.writeback_policy == "disabled"


def test_compile_event_route_api_exposes_plan() -> None:
    app = FastAPI()
    app.include_router(create_integration_event_routes_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-event-routes/compile",
        json={
            "provider": "Zendesk",
            "event_type": "ticket.updated",
            "subject_id": "ticket-1001",
            "workspace_id": "support",
            "desired_outcome": "summarize the customer issue and draft a response",
            "writeback": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["provider"] == "zendesk"
    assert body["plan"]["required_capability"] == "support.ticket_resolution"
    assert body["plan"]["writeback_policy"] == "requires_approval"
    assert body["plan"]["idempotency_key"] == (
        "integration-route:zendesk:support:ticket.updated:ticket-1001"
    )
