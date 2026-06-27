"""Integration sandbox fixture compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_sandbox_fixtures import (
    compile_integration_sandbox_fixtures,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_sandbox_fixtures():
    bundle = compile_integration_sandbox_fixtures("archon-style-workflow-blueprints")

    assert bundle["object"] == "integration_sandbox_fixture_bundle"
    assert bundle["capability_slug"] == "archon-style-workflow-blueprints"
    assert bundle["mode"] == "credentialless"
    assert bundle["risk_tier"] == "internal_harness"
    assert bundle["fixtures"][0] == {
        "id": "workflow-blueprint-phase-graph",
        "title": "Compile a deterministic workflow phase graph",
        "provider_event": "workflow_blueprint.submitted",
        "expected_signal_type": "integration.workflow_blueprint.received",
        "assertions": [
            "phase ids are stable across repeated compiles",
            "typed inputs and outputs are present for each phase",
            "terminal phases declare completion evidence requirements",
        ],
    }
    assert "no live credentials required" in bundle["operator_notes"]


def test_compiles_provider_specific_sandbox_fixtures():
    bundle = compile_integration_sandbox_fixtures("linear-jira-work-intake")

    assert bundle["risk_tier"] == "external_write"
    assert [fixture["id"] for fixture in bundle["fixtures"]] == [
        "work-item-created",
        "work-item-status-changed",
        "work-item-comment-added",
    ]
    assert bundle["fixtures"][0]["expected_signal_type"] == "integration.work_item.created"
    assert bundle["fixtures"][1]["assertions"] == [
        "external status maps to an allowed Omnigent Task lifecycle state",
        "source-of-truth ownership is retained on the provider object",
        "write-back idempotency key is derived from provider item id and transition id",
    ]


def test_unknown_capability_sandbox_fixtures_returns_none():
    assert compile_integration_sandbox_fixtures("missing") is None


def test_integration_capability_route_exposes_sandbox_fixtures():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/slack-command-center/sandbox-fixtures"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "slack-command-center"
    assert payload["fixtures"][0]["id"] == "communication-message-received"
    assert payload["fixtures"][0]["expected_signal_type"] == "integration.message.received"

    missing = client.get("/v1/integration-capabilities/not-real/sandbox-fixtures")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
