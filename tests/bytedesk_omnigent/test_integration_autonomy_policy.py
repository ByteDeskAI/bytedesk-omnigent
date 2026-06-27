"""Integration autonomy policy compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_autonomy_policy import (
    compile_integration_autonomy_policy,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_internal_workflow_harness_policy():
    policy = compile_integration_autonomy_policy("archon-style-workflow-blueprints")

    assert policy is not None
    assert policy["capability_slug"] == "archon-style-workflow-blueprints"
    assert policy["autonomy_level"] == "deterministic_internal"
    assert policy["risk_tier"] == "internal_harness"
    assert policy["requires_human_approval"] is False
    assert "compile workflow phases into Omnigent Tasks" in policy["allowed_actions"]
    assert policy["approval_required_for"] == []
    assert "directly accessing third-party customer data" in policy["forbidden_actions"]


def test_compiles_external_write_policy_with_mutation_guards():
    policy = compile_integration_autonomy_policy("slack-command-center")

    assert policy is not None
    assert policy["autonomy_level"] == "supervised_external_write"
    assert policy["risk_tier"] == "external_write"
    assert policy["requires_human_approval"] is True
    assert "chat:write" in policy["write_scopes"]
    assert "posting outbound messages to Slack command center" in policy["approval_required_for"]
    assert (
        "mutating provider records without an Omnigent approval record"
        in policy["forbidden_actions"]
    )
    assert "Slack command center requests write-capable scopes" in policy["rationale"]


def test_unknown_capability_policy_returns_none():
    assert compile_integration_autonomy_policy("missing") is None


def test_integration_capability_route_exposes_autonomy_policy():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/google-workspace-operator/autonomy-policy")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["category"] == "knowledge"
    assert payload["autonomy_level"] == "supervised_external_write"
    assert "broad knowledge access beyond selected files, pages, or datasets" in payload[
        "approval_required_for"
    ]

    missing = client.get("/v1/integration-capabilities/not-real/autonomy-policy")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
