"""Integration tenant routing manifest tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_tenant_routing import (
    compile_integration_tenant_routing_manifest,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_workflow_harness_tenant_routing_manifest():
    manifest = compile_integration_tenant_routing_manifest(
        "archon-style-workflow-blueprints"
    )

    assert manifest["object"] == "integration_tenant_routing_manifest"
    assert manifest["capability_slug"] == "archon-style-workflow-blueprints"
    assert manifest["routing_mode"] == "internal_workflow_namespace"
    assert manifest["workspace_identity_fields"] == [
        "tenant_id",
        "workflow_blueprint_id",
        "workflow_run_id",
    ]
    assert manifest["default_signal_routes"][0] == {
        "source_event": "workflow.phase.completed",
        "target_queue": "omnigent.workflow.harness",
        "coordination_goal": "advance deterministic phase graph",
    }
    assert "tenant namespace cannot read another tenant's workflow runs" in manifest[
        "isolation_checks"
    ]


def test_compiles_external_provider_tenant_routing_manifest():
    manifest = compile_integration_tenant_routing_manifest("slack-command-center")

    assert manifest["routing_mode"] == "external_workspace_mapping"
    assert manifest["workspace_identity_fields"] == [
        "tenant_id",
        "provider_workspace_id",
        "provider_actor_id",
    ]
    assert manifest["default_signal_routes"][-1] == {
        "source_event": "communication.approval_requested",
        "target_queue": "omnigent.human_approval",
        "coordination_goal": "pause autonomous write until approved",
    }
    assert manifest["audit_tags"] == [
        "capability:slack-command-center",
        "category:communication",
        "risk:external_write",
    ]


def test_unknown_capability_returns_none():
    assert compile_integration_tenant_routing_manifest("missing") is None


def test_integration_capability_route_exposes_tenant_routing_manifest():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/linear-jira-work-intake/tenant-routing-manifest"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "linear-jira-work-intake"
    assert payload["category"] == "project_management"
    assert payload["default_signal_routes"][0]["target_queue"] == "omnigent.tasks.intake"

    missing = client.get(
        "/v1/integration-capabilities/not-real/tenant-routing-manifest"
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
