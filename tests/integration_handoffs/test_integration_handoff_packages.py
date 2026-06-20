"""Tests for deterministic integration handoff package compilation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_handoff_packages import (
    compile_integration_handoff_package,
)
from bytedesk_omnigent.routes.integration_handoff_packages import (
    create_integration_handoff_packages_router,
)


def test_compile_handoff_package_normalizes_external_event_into_agent_ready_brief():
    package = compile_integration_handoff_package(
        provider="GitHub",
        workspace_id="ws_123",
        event_type="pull_request.opened",
        external_id="PR-42",
        actor="octocat",
        title="Review payment retry PR",
        url="https://github.com/acme/app/pull/42",
        payload={
            "repository": {"full_name": "acme/app"},
            "pull_request": {"number": 42},
        },
        requested_capabilities=["code-review", "python"],
    )

    assert package.provider == "github"
    assert package.workspace_id == "ws_123"
    assert package.correlation_id == (
        "integration-handoff:v1:github:ws_123:pull_request.opened:PR-42"
    )
    assert package.agent_brief == {
        "title": "Review payment retry PR",
        "summary": "GitHub pull_request.opened from octocat needs autonomous follow-up.",
        "source_url": "https://github.com/acme/app/pull/42",
    }
    assert package.routing == {
        "requested_capabilities": ["code-review", "python"],
        "recommended_agent_type": "code-reviewer",
        "priority": "normal",
    }
    assert package.workflow_steps == [
        "normalize_external_context",
        "select_or_create_agent",
        "hydrate_agent_brief",
        "execute_agent_task",
        "record_outcome",
        "write_back_to_provider",
    ]
    assert package.acceptance_checks == [
        "source_event_is_traceable",
        "agent_brief_has_title_and_summary",
        "provider_writeback_is_idempotent",
    ]
    assert package.payload_excerpt == {
        "repository": {"full_name": "acme/app"},
        "pull_request": {"number": 42},
    }


def test_compile_handoff_package_defaults_title_and_recommends_sales_agent():
    package = compile_integration_handoff_package(
        provider="HubSpot",
        workspace_id="ws_9",
        event_type="deal.created",
        external_id="deal-77",
        actor=None,
        title=None,
        url=None,
        payload={"dealId": "deal-77"},
        requested_capabilities=[],
    )

    assert package.agent_brief["title"] == "Handle HubSpot deal.created"
    assert package.agent_brief["summary"] == "HubSpot deal.created needs autonomous follow-up."
    assert package.routing["recommended_agent_type"] == "revenue-operations-agent"
    assert package.routing["priority"] == "high"
    assert package.payload_excerpt == {"dealId": "deal-77"}


def test_handoff_package_compile_route_returns_serializable_contract():
    app = FastAPI()
    router = create_integration_handoff_packages_router()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/integration-handoff-packages/compile",
        json={
            "provider": "Slack",
            "workspace_id": "ws_chat",
            "event_type": "app_mention",
            "external_id": "evt_1",
            "actor": "U123",
            "title": "Answer enterprise onboarding question",
            "url": "https://slack.com/app_redirect?channel=C123",
            "payload": {"channel": "C123", "text": "Can an agent handle onboarding?"},
            "requested_capabilities": ["customer-support"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handoff_package"]["provider"] == "slack"
    assert body["handoff_package"]["correlation_id"] == (
        "integration-handoff:v1:slack:ws_chat:app_mention:evt_1"
    )
    assert body["handoff_package"]["routing"]["recommended_agent_type"] == (
        "support-agent"
    )
