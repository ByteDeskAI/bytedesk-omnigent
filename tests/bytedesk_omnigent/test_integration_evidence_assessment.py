"""Integration evidence assessment compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_evidence_assessment import (
    IntegrationEvidenceItem,
    assess_integration_evidence,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_assesses_gate_evidence_against_verification_matrix():
    assessment = assess_integration_evidence(
        "slack-command-center",
        evidence_items=(
            IntegrationEvidenceItem(
                gate_id="catalog-contract",
                evidence=(
                    "capability slug resolves in the integration catalog",
                    "auth model and required scopes are documented",
                    "business case and future unlocks are present",
                ),
                source="loop-83-test",
            ),
            IntegrationEvidenceItem(
                gate_id="auth-boundary",
                evidence=("requested scopes match the catalog entry",),
                source="loop-83-test",
            ),
        ),
    )

    assert assessment is not None
    assert assessment["capability_slug"] == "slack-command-center"
    assert assessment["risk_tier"] == "external_write"
    assert assessment["ready_for_activation"] is False
    assert assessment["satisfied_gate_count"] == 1
    assert assessment["total_gate_count"] == 8
    assert (
        assessment["missing_evidence_count"]
        == assessment["minimum_required_evidence_count"] - 4
    )

    catalog_gate = assessment["gate_results"][0]
    assert catalog_gate == {
        "gate_id": "catalog-contract",
        "title": "Catalog contract is explicit and stable",
        "satisfied": True,
        "provided_evidence": [
            "capability slug resolves in the integration catalog",
            "auth model and required scopes are documented",
            "business case and future unlocks are present",
        ],
        "missing_evidence": [],
        "sources": ["loop-83-test"],
    }

    auth_gate = assessment["gate_results"][1]
    assert auth_gate["satisfied"] is False
    assert auth_gate["provided_evidence"] == ["requested scopes match the catalog entry"]
    assert auth_gate["missing_evidence"] == [
        "credential storage path is secret-manager backed or explicitly inert",
        "token refresh or re-authorization path is documented",
    ]


def test_assessment_is_ready_when_every_required_evidence_item_is_present():
    assessment = assess_integration_evidence(
        "archon-style-workflow-blueprints",
        evidence_items=(
            IntegrationEvidenceItem(
                gate_id=gate_id,
                evidence=tuple(evidence),
                source="deterministic-certification-run",
            )
            for gate_id, evidence in {
                "catalog-contract": (
                    "capability slug resolves in the integration catalog",
                    "auth model and required scopes are documented",
                    "business case and future unlocks are present",
                ),
                "auth-boundary": (
                    "requested scopes match the catalog entry",
                    "credential storage path is secret-manager backed or explicitly inert",
                    "token refresh or re-authorization path is documented",
                ),
                "ingress-normalization": (
                    "external event id is preserved for traceability",
                    "tenant or workspace id is retained for routing",
                    "unsupported event types fail closed with an auditable reason",
                ),
                "idempotency-replay": (
                    "idempotency key is derived from stable provider identifiers",
                    "duplicate delivery returns the same normalized outcome",
                    "retry schedule and terminal failure behavior are declared",
                ),
                "policy-approval": (
                    "read-only actions are separated from write actions",
                    "high-risk writes name the required approval strategy",
                    "denied approvals leave no provider-side mutation",
                ),
                "observability-evidence": (
                    "task id, provider object id, and agent id are correlated",
                    "success and failure paths produce outcome records",
                    "operator-facing status is safe to expose without secrets",
                ),
                "rollback-readiness": (
                    "connector can be disabled without deleting historical evidence",
                    "webhook or subscription teardown steps are documented",
                    "manual recovery owner and escalation path are named",
                ),
                "workflow-determinism": (
                    "phase graph uses stable node ids",
                    "typed inputs and outputs are declared per phase",
                    "completion evidence is captured for every terminal phase",
                ),
            }.items()
        ),
    )

    assert assessment is not None
    assert assessment["risk_tier"] == "internal_harness"
    assert assessment["ready_for_activation"] is True
    assert assessment["satisfied_gate_count"] == assessment["total_gate_count"]
    assert assessment["missing_evidence_count"] == 0


def test_unknown_capability_assessment_returns_none():
    assert assess_integration_evidence("missing", evidence_items=()) is None


def test_integration_capability_route_exposes_evidence_assessment_preview():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-capabilities/google-workspace-operator/evidence-assessment",
        json={
            "evidence_items": [
                {
                    "gate_id": "knowledge-scope-control",
                    "evidence": ["read set is constrained to selected files, pages, or databases"],
                    "source": "platform-preview",
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["ready_for_activation"] is False
    assert payload["gate_results"][-1]["gate_id"] == "knowledge-scope-control"
    assert payload["gate_results"][-1]["provided_evidence"] == [
        "read set is constrained to selected files, pages, or databases"
    ]

    missing = client.post(
        "/v1/integration-capabilities/not-real/evidence-assessment",
        json={"evidence_items": []},
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
