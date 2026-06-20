"""Integration verification matrix compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_verification_matrix():
    matrix = compile_integration_verification_matrix("archon-style-workflow-blueprints")

    assert matrix["capability_slug"] == "archon-style-workflow-blueprints"
    assert matrix["category"] == "workflow_harness"
    assert matrix["risk_tier"] == "internal_harness"
    assert [gate["id"] for gate in matrix["gates"]] == [
        "catalog-contract",
        "auth-boundary",
        "ingress-normalization",
        "idempotency-replay",
        "policy-approval",
        "observability-evidence",
        "rollback-readiness",
        "workflow-determinism",
    ]
    workflow_gate = matrix["gates"][-1]
    assert workflow_gate["required_evidence"] == [
        "phase graph uses stable node ids",
        "typed inputs and outputs are declared per phase",
        "completion evidence is captured for every terminal phase",
    ]
    assert matrix["minimum_required_evidence_count"] == sum(
        len(gate["required_evidence"]) for gate in matrix["gates"]
    )


def test_compiles_provider_specific_verification_matrix():
    matrix = compile_integration_verification_matrix("slack-command-center")

    assert matrix["risk_tier"] == "external_write"
    assert matrix["auth_model"] == "OAuth 2.0 bot + user tokens"
    assert matrix["required_scopes"] == [
        "channels:history",
        "chat:write",
        "commands",
        "users:read",
    ]
    assert matrix["gates"][-1] == {
        "id": "communication-loop",
        "title": "Human collaboration loop is bounded and auditable",
        "required_evidence": [
            "agent replies can be correlated to source thread or channel",
            "approval or escalation prompts include actor and task context",
            "outbound messages are rate-limited per workspace or channel",
        ],
    }


def test_unknown_capability_returns_none():
    assert compile_integration_verification_matrix("missing") is None


def test_integration_capability_route_exposes_verification_matrix():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/google-workspace-operator/verification-matrix"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["category"] == "knowledge"
    assert payload["gates"][-1]["id"] == "knowledge-scope-control"

    missing = client.get("/v1/integration-capabilities/not-real/verification-matrix")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
