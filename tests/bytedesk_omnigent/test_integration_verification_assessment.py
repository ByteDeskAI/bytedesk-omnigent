"""Integration verification evidence assessment tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_verification_assessment import (
    assess_integration_verification_evidence,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_assessment_flags_missing_gate_evidence_and_counts_completion():
    assessment = assess_integration_verification_evidence(
        "slack-command-center",
        provided_evidence={
            "catalog-contract": [
                "capability slug resolves in the integration catalog",
                "auth model and required scopes are documented",
            ],
            "auth-boundary": [
                "requested scopes match the catalog entry",
                "credential storage path is secret-manager backed or explicitly inert",
                "token refresh or re-authorization path is documented",
            ],
        },
    )

    assert assessment is not None
    assert assessment["capability_slug"] == "slack-command-center"
    assert assessment["status"] == "incomplete"
    assert assessment["provided_evidence_count"] == 5
    assert assessment["minimum_required_evidence_count"] > assessment["provided_evidence_count"]
    assert assessment["gate_assessments"][0] == {
        "gate_id": "catalog-contract",
        "title": "Catalog contract is explicit and stable",
        "status": "incomplete",
        "provided_evidence": [
            "capability slug resolves in the integration catalog",
            "auth model and required scopes are documented",
        ],
        "missing_evidence": ["business case and future unlocks are present"],
    }
    assert assessment["gate_assessments"][1]["status"] == "complete"


def test_assessment_marks_complete_when_all_required_evidence_is_provided():
    matrix = assess_integration_verification_evidence(
        "archon-style-workflow-blueprints",
        provided_evidence={
            "catalog-contract": [
                "capability slug resolves in the integration catalog",
                "auth model and required scopes are documented",
                "business case and future unlocks are present",
            ],
            "auth-boundary": [
                "requested scopes match the catalog entry",
                "credential storage path is secret-manager backed or explicitly inert",
                "token refresh or re-authorization path is documented",
            ],
            "ingress-normalization": [
                "external event id is preserved for traceability",
                "tenant or workspace id is retained for routing",
                "unsupported event types fail closed with an auditable reason",
            ],
            "idempotency-replay": [
                "idempotency key is derived from stable provider identifiers",
                "duplicate delivery returns the same normalized outcome",
                "retry schedule and terminal failure behavior are declared",
            ],
            "policy-approval": [
                "read-only actions are separated from write actions",
                "high-risk writes name the required approval strategy",
                "denied approvals leave no provider-side mutation",
            ],
            "observability-evidence": [
                "task id, provider object id, and agent id are correlated",
                "success and failure paths produce outcome records",
                "operator-facing status is safe to expose without secrets",
            ],
            "rollback-readiness": [
                "connector can be disabled without deleting historical evidence",
                "webhook or subscription teardown steps are documented",
                "manual recovery owner and escalation path are named",
            ],
            "workflow-determinism": [
                "phase graph uses stable node ids",
                "typed inputs and outputs are declared per phase",
                "completion evidence is captured for every terminal phase",
            ],
        },
    )

    assert matrix is not None
    assert matrix["status"] == "complete"
    assert matrix["missing_evidence_count"] == 0
    assert {gate["status"] for gate in matrix["gate_assessments"]} == {"complete"}


def test_assessment_returns_none_for_unknown_capability():
    assert assess_integration_verification_evidence("missing", provided_evidence={}) is None


def test_integration_capability_route_exposes_verification_assessment():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-capabilities/slack-command-center/verification-assessment",
        json={
            "provided_evidence": {
                "catalog-contract": [
                    "capability slug resolves in the integration catalog",
                    "business case and future unlocks are present",
                ]
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "slack-command-center"
    assert payload["status"] == "incomplete"
    assert payload["gate_assessments"][0]["missing_evidence"] == [
        "auth model and required scopes are documented"
    ]

    missing = client.post(
        "/v1/integration-capabilities/not-real/verification-assessment",
        json={"provided_evidence": {}},
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


def test_integration_capability_route_rejects_malformed_evidence_payloads():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/integration-capabilities/slack-command-center/verification-assessment",
        json={"provided_evidence": []},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_evidence_payload"

    nested = client.post(
        "/v1/integration-capabilities/slack-command-center/verification-assessment",
        json={"provided_evidence": {"catalog-contract": 1}},
    )

    assert nested.status_code == 422
    assert nested.json()["error"] == "invalid_evidence_payload"
