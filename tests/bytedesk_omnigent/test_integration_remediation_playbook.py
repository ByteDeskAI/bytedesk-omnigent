"""Integration remediation playbook compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_remediation_playbook import (
    compile_integration_remediation_playbook,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_remediation_playbook_from_failed_verification_gates():
    playbook = compile_integration_remediation_playbook(
        "slack-command-center",
        failed_gate_ids=("auth-boundary", "communication-loop"),
    )

    assert playbook is not None
    assert playbook["object"] == "integration_remediation_playbook"
    assert playbook["capability_slug"] == "slack-command-center"
    assert playbook["risk_tier"] == "external_write"
    assert playbook["failed_gate_ids"] == ["auth-boundary", "communication-loop"]
    assert playbook["summary"]["total_failed_gates"] == 2
    assert playbook["summary"]["requires_human_approval"] is True
    assert [step["gate_id"] for step in playbook["steps"]] == [
        "auth-boundary",
        "communication-loop",
    ]
    assert playbook["steps"][0]["owner"] == "integration-security-owner"
    assert (
        "requested scopes match the catalog entry"
        in playbook["steps"][0]["evidence_to_collect"]
    )
    assert playbook["steps"][1]["owner"] == "workspace-operations-owner"
    assert playbook["steps"][1]["recommended_actions"][-1] == (
        "rerun verification matrix gate communication-loop and attach evidence before promotion"
    )


def test_unknown_or_unknown_failed_gate_returns_none_or_structured_error():
    assert compile_integration_remediation_playbook("missing") is None

    playbook = compile_integration_remediation_playbook(
        "github-engineering-copilot",
        failed_gate_ids=("not-a-gate",),
    )

    assert playbook is not None
    assert playbook["steps"] == []
    assert playbook["unknown_failed_gate_ids"] == ["not-a-gate"]
    assert playbook["summary"]["total_failed_gates"] == 0


def test_integration_capability_route_exposes_remediation_playbook():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/archon-style-workflow-blueprints/remediation-playbook",
        params={"failed_gate_id": ["workflow-determinism", "idempotency-replay"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "archon-style-workflow-blueprints"
    assert payload["risk_tier"] == "internal_harness"
    assert payload["failed_gate_ids"] == ["workflow-determinism", "idempotency-replay"]
    assert payload["steps"][0]["owner"] == "workflow-harness-owner"

    missing = client.get(
        "/v1/integration-capabilities/not-real/remediation-playbook",
        params={"failed_gate_id": "auth-boundary"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
