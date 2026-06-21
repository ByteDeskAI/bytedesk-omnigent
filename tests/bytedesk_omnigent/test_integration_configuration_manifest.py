"""Integration configuration manifest compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_configuration_manifest import (
    compile_integration_configuration_manifest,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_oauth_configuration_manifest_without_secret_values():
    manifest = compile_integration_configuration_manifest("slack-command-center")

    assert manifest["object"] == "integration_configuration_manifest"
    assert manifest["capability_slug"] == "slack-command-center"
    assert manifest["auth_model"] == "OAuth 2.0 bot + user tokens"
    assert manifest["configuration_keys"] == [
        "SLACK_COMMAND_CENTER_CLIENT_ID",
        "SLACK_COMMAND_CENTER_CLIENT_SECRET",
        "SLACK_COMMAND_CENTER_REDIRECT_URI",
        "SLACK_COMMAND_CENTER_SIGNING_SECRET",
        "SLACK_COMMAND_CENTER_WEBHOOK_BASE_URL",
    ]
    secret_slots = [slot for slot in manifest["slots"] if slot["secret"]]
    assert [slot["key"] for slot in secret_slots] == [
        "SLACK_COMMAND_CENTER_CLIENT_SECRET",
        "SLACK_COMMAND_CENTER_SIGNING_SECRET",
    ]
    assert all("value" not in slot for slot in manifest["slots"])
    assert manifest["minimum_required_slots"] == 5


def test_compiles_internal_workflow_harness_manifest_without_secret_slots():
    manifest = compile_integration_configuration_manifest(
        "archon-style-workflow-blueprints"
    )

    assert manifest["capability_slug"] == "archon-style-workflow-blueprints"
    assert manifest["configuration_keys"] == [
        "ARCHON_STYLE_WORKFLOW_BLUEPRINTS_BLUEPRINT_REPOSITORY",
        "ARCHON_STYLE_WORKFLOW_BLUEPRINTS_SCHEMA_VERSION",
        "ARCHON_STYLE_WORKFLOW_BLUEPRINTS_ARTIFACT_BUCKET",
    ]
    assert not any(slot["secret"] for slot in manifest["slots"])
    assert manifest["deployment_notes"] == [
        "Validate blueprint schema before admitting a workflow template.",
        "Store run artifacts where task, agent, and phase evidence can be correlated.",
    ]


def test_unknown_capability_configuration_manifest_returns_none():
    assert compile_integration_configuration_manifest("missing") is None


def test_integration_capability_route_exposes_configuration_manifest():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/google-workspace-operator/configuration-manifest"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert "GOOGLE_WORKSPACE_OPERATOR_CLIENT_ID" in payload["configuration_keys"]
    assert "GOOGLE_WORKSPACE_OPERATOR_WEBHOOK_BASE_URL" not in payload["configuration_keys"]

    missing = client.get(
        "/v1/integration-capabilities/not-real/configuration-manifest"
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
