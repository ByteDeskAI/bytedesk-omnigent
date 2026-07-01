"""Integration telemetry contract compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_telemetry_contract import (
    compile_integration_telemetry_contract,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_workflow_harness_telemetry_contract():
    contract = compile_integration_telemetry_contract(
        "archon-style-workflow-blueprints"
    )

    assert contract is not None
    assert contract["object"] == "integration_telemetry_contract"
    assert contract["capability_slug"] == "archon-style-workflow-blueprints"
    assert contract["risk_tier"] == "internal_harness"
    assert contract["metric_prefix"] == "omnigent.integration.archon_style_workflow_blueprints"
    assert contract["required_trace_fields"] == [
        "tenant_id",
        "capability_slug",
        "workflow_id",
        "phase_id",
        "task_id",
        "agent_id",
        "evidence_id",
    ]
    assert [event["event"] for event in contract["events"]] == [
        "integration.workflow.phase_started",
        "integration.workflow.phase_completed",
        "integration.workflow.phase_failed",
    ]
    assert contract["health_indicators"][0]["metric"] == (
        "omnigent.integration.archon_style_workflow_blueprints.phase_success_rate"
    )


def test_compiles_external_write_telemetry_contract_with_policy_events():
    contract = compile_integration_telemetry_contract("slack-command-center")

    assert contract is not None
    assert contract["risk_tier"] == "external_write"
    assert "provider_workspace_id" in contract["required_trace_fields"]
    assert "provider_event_id" in contract["required_trace_fields"]
    assert [event["event"] for event in contract["events"]] == [
        "integration.ingress.received",
        "integration.ingress.normalized",
        "integration.action.policy_checked",
        "integration.action.dispatched",
        "integration.action.failed",
    ]
    policy_event = contract["events"][2]
    assert policy_event["required_fields"] == [
        "tenant_id",
        "capability_slug",
        "task_id",
        "agent_id",
        "action_id",
        "approval_strategy",
        "policy_decision",
    ]
    assert contract["health_indicators"][0] == {
        "metric": "omnigent.integration.slack_command_center.normalization_success_rate",
        "target": ">= 99% over 24h",
        "owner": "integration-operator",
    }


def test_unknown_capability_telemetry_contract_returns_none():
    assert compile_integration_telemetry_contract("missing") is None


def test_integration_capability_route_exposes_telemetry_contract():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/telemetry-contract"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["risk_tier"] == "external_write"
    assert payload["metric_prefix"] == "omnigent.integration.github_engineering_copilot"

    missing = client.get("/v1/integration-capabilities/not-real/telemetry-contract")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
