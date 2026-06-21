"""Integration ownership matrix compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_ownership_matrix import (
    compile_integration_ownership_matrix,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_internal_workflow_ownership_matrix():
    matrix = compile_integration_ownership_matrix("archon-style-workflow-blueprints")

    assert matrix is not None
    assert matrix["capability_slug"] == "archon-style-workflow-blueprints"
    assert matrix["risk_tier"] == "internal_harness"
    assert matrix["required_approvers"] == ["workflow_owner", "platform_operator"]
    assert matrix["external_participants"] == []
    assert [lane["id"] for lane in matrix["lanes"]] == [
        "workflow-owner",
        "platform-operator",
        "security-reviewer",
    ]
    assert matrix["handoff_checklist"] == [
        "Publish the deterministic workflow blueprint and phase graph.",
        "Bind every phase to an owning agent role and completion evidence.",
        "Record rollback and disablement owner before activation.",
    ]


def test_compiles_external_write_ownership_matrix():
    matrix = compile_integration_ownership_matrix("slack-command-center")

    assert matrix is not None
    assert matrix["risk_tier"] == "external_write"
    assert matrix["required_approvers"] == [
        "workspace_admin",
        "security_reviewer",
        "business_owner",
    ]
    assert matrix["external_participants"] == ["Slack workspace admin"]
    assert matrix["lanes"][0] == {
        "id": "business-owner",
        "title": "Business owner",
        "responsibilities": [
            "Confirm the integration business case and success metric.",
            "Name the escalation owner for blocked or failed autonomous work.",
        ],
    }
    assert (
        "Validate least-privilege OAuth scopes: channels:history, chat:write, "
        "commands, users:read."
    ) in matrix["handoff_checklist"]


def test_unknown_capability_ownership_matrix_returns_none():
    assert compile_integration_ownership_matrix("missing") is None


def test_integration_capability_route_exposes_ownership_matrix():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/google-workspace-operator/ownership-matrix"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["category"] == "knowledge"
    assert payload["external_participants"] == ["Google Workspace administrator"]
    assert "knowledge-steward" in [lane["id"] for lane in payload["lanes"]]

    missing = client.get("/v1/integration-capabilities/not-real/ownership-matrix")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
