"""Integration readiness assessment tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_readiness_assessment import (
    compile_integration_readiness_assessment,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_assessment_scores_submitted_evidence_against_verification_matrix():
    assessment = compile_integration_readiness_assessment(
        "slack-command-center",
        evidence={
            "catalog-contract": (
                "capability slug resolves in the integration catalog",
                "auth model and required scopes are documented",
                "business case and future unlocks are present",
            ),
            "communication-loop": (
                "agent replies can be correlated to source thread or channel",
                "approval or escalation prompts include actor and task context",
            ),
        },
    )

    assert assessment is not None
    assert assessment["capability_slug"] == "slack-command-center"
    assert assessment["risk_tier"] == "external_write"
    assert assessment["activation_state"] == "blocked_by_policy_evidence"
    assert assessment["satisfied_gate_count"] == 1
    assert assessment["total_gate_count"] == 8
    assert assessment["submitted_evidence_count"] == 5
    assert assessment["satisfied_evidence_count"] == 5
    assert assessment["missing_evidence_count"] == 19
    assert assessment["readiness_percent"] == 21
    assert assessment["next_missing_gate_id"] == "auth-boundary"
    assert assessment["gates"][0]["status"] == "satisfied"
    assert assessment["gates"][-1]["status"] == "partial"
    assert assessment["gates"][-1]["missing_evidence"] == [
        "outbound messages are rate-limited per workspace or channel"
    ]


def test_assessment_marks_internal_harness_ready_when_all_evidence_is_present():
    matrix_evidence = {
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
    }

    assessment = compile_integration_readiness_assessment(
        "archon-style-workflow-blueprints",
        evidence=matrix_evidence,
    )

    assert assessment is not None
    assert assessment["activation_state"] == "ready"
    assert assessment["risk_tier"] == "internal_harness"
    assert assessment["readiness_percent"] == 100
    assert assessment["satisfied_gate_count"] == assessment["total_gate_count"]
    assert assessment["next_missing_gate_id"] is None


def test_unknown_capability_assessment_returns_none():
    assert compile_integration_readiness_assessment("missing", evidence={}) is None


def test_integration_capability_route_exposes_readiness_assessment():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-capabilities/google-workspace-operator/readiness-assessment",
        json={
            "evidence": {
                "catalog-contract": [
                    "capability slug resolves in the integration catalog",
                    "auth model and required scopes are documented",
                    "business case and future unlocks are present",
                ]
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "google-workspace-operator"
    assert payload["activation_state"] == "in_progress"
    assert payload["gates"][0]["status"] == "satisfied"

    missing = client.post(
        "/v1/integration-capabilities/not-real/readiness-assessment",
        json={"evidence": {}},
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
