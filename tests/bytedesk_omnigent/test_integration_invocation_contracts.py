"""Integration invocation contract compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_invocation_contracts import (
    compile_integration_invocation_contract,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_invocation_contract_for_internal_harness():
    contract = compile_integration_invocation_contract(
        "archon-style-workflow-blueprints",
        requester="office.workflow_designer",
        context_refs=("office.workflow:launch-template", "agent.role:qa-reviewer"),
        idempotency_key="tenant-a:workflow-blueprint:launch-template:v1",
    )

    assert contract is not None
    assert contract["object"] == "integration_invocation_contract"
    assert contract["capability_slug"] == "archon-style-workflow-blueprints"
    assert contract["requester"] == "office.workflow_designer"
    assert contract["idempotency_key"] == "tenant-a:workflow-blueprint:launch-template:v1"
    assert contract["execution_mode"] == "workflow_harness"
    assert contract["approval_mode"] == "operator_review"
    assert contract["required_context_refs"] == [
        "tenant",
        "requester",
        "goal",
        "workflow_blueprint",
        "phase_graph",
    ]
    assert contract["missing_context_refs"] == ["tenant", "requester", "goal"]
    assert contract["routing_hints"] == [
        "compile deterministic phase graph before agent dispatch",
        "bind every terminal phase to completion evidence",
        "prefer tool nodes for deterministic steps and agent nodes for judgment",
    ]
    assert contract["verification_matrix_path"] == (
        "/v1/integration-capabilities/archon-style-workflow-blueprints/verification-matrix"
    )


def test_compiles_external_write_contract_with_mutation_approval():
    contract = compile_integration_invocation_contract(
        "slack-command-center",
        requester="slack.workspace:T123",
        context_refs=("tenant", "requester", "goal", "slack.channel:C456"),
        idempotency_key="T123:C456:thread:789",
    )

    assert contract is not None
    assert contract["execution_mode"] == "connected_app"
    assert contract["risk_tier"] == "external_write"
    assert contract["approval_mode"] == "approval_required_for_mutations"
    assert "source_event" in contract["missing_context_refs"]
    assert contract["routing_hints"][0] == "normalize provider event into an Omnigent signal"
    assert contract["activity_projection"]["status_channel"] == "slack-command-center.status"


def test_unknown_capability_returns_none():
    assert compile_integration_invocation_contract(
        "missing",
        requester="office",
        context_refs=("tenant",),
        idempotency_key="key",
    ) is None


def test_integration_capability_route_exposes_invocation_contract():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-capabilities/google-workspace-operator/invocation-contract",
        json={
            "requester": "office.room:abc",
            "context_refs": ["tenant", "requester", "goal", "google.drive.file:123"],
            "idempotency_key": "tenant:drive-file-123:summary",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["approval_mode"] == "approval_required_for_mutations"
    assert payload["missing_context_refs"] == ["source_event"]

    missing = client.post(
        "/v1/integration-capabilities/not-real/invocation-contract",
        json={
            "requester": "office.room:abc",
            "context_refs": ["tenant"],
            "idempotency_key": "tenant:not-real",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
