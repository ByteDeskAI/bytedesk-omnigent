"""Integration data boundary manifest compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_data_boundary import (
    compile_integration_data_boundary,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_data_boundary_for_slack_command_center():
    manifest = compile_integration_data_boundary("slack-command-center")

    assert manifest is not None
    assert manifest["object"] == "integration_data_boundary_manifest"
    assert manifest["capability_slug"] == "slack-command-center"
    assert manifest["category"] == "communication"
    assert manifest["risk_tier"] == "external_write"
    assert manifest["inbound_data_classes"] == [
        "workspace_id",
        "channel_id",
        "thread_ts",
        "user_profile",
        "message_text",
    ]
    assert manifest["outbound_mutation_classes"] == [
        "post_message",
        "thread_reply",
        "approval_prompt",
    ]
    assert manifest["secret_boundaries"] == [
        "OAuth tokens stay in the configured secret backend and are never included "
        "in task payloads.",
        "Webhook signatures or verification secrets are compared at ingress and "
        "redacted from evidence.",
    ]
    assert "workspace_id" in manifest["required_audit_fields"]
    assert manifest["retention_policy"] == (
        "Retain normalized task/evidence metadata; do not retain raw provider "
        "payloads beyond replay/debug windows."
    )


def test_compiles_internal_workflow_boundary_without_external_mutations():
    manifest = compile_integration_data_boundary("archon-style-workflow-blueprints")

    assert manifest is not None
    assert manifest["risk_tier"] == "internal_harness"
    assert manifest["inbound_data_classes"] == [
        "workflow_blueprint_id",
        "phase_inputs",
        "agent_role",
        "verification_evidence",
    ]
    assert manifest["outbound_mutation_classes"] == ["create_task", "record_evidence"]
    assert manifest["secret_boundaries"] == [
        "Workflow definitions may reference secret names only; resolved secret values "
        "stay in the runtime secret backend."
    ]


def test_unknown_capability_data_boundary_returns_none():
    assert compile_integration_data_boundary("missing") is None


def test_integration_capability_route_exposes_data_boundary_manifest():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/github-engineering-copilot/data-boundary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["inbound_data_classes"] == [
        "repository_id",
        "issue_or_pr_id",
        "commit_sha",
        "check_run_state",
        "review_comment",
    ]

    missing = client.get("/v1/integration-capabilities/not-real/data-boundary")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
