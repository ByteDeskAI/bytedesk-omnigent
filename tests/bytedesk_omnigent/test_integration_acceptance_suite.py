"""Integration acceptance suite compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_acceptance_suite import (
    compile_integration_acceptance_suite,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_acceptance_suite():
    suite = compile_integration_acceptance_suite("archon-style-workflow-blueprints")

    assert suite["object"] == "integration_acceptance_suite"
    assert suite["capability_slug"] == "archon-style-workflow-blueprints"
    assert suite["risk_tier"] == "internal_harness"
    assert suite["minimum_passing_scenarios"] == 4
    assert [scenario["id"] for scenario in suite["scenarios"]] == [
        "catalog-contract-loads",
        "auth-boundary-is-declared",
        "workflow-happy-path",
        "workflow-phase-fail-closed",
    ]
    workflow_scenario = suite["scenarios"][2]
    assert workflow_scenario["mode"] == "happy_path"
    assert workflow_scenario["expected_evidence"] == [
        "stable phase node ids are preserved from input to compiled task graph",
        "typed phase inputs and outputs are present for every compiled node",
        "terminal completion evidence names the responsible agent role",
    ]


def test_compiles_provider_acceptance_suite_with_write_and_replay_safety():
    suite = compile_integration_acceptance_suite("slack-command-center")

    assert suite["risk_tier"] == "external_write"
    assert suite["provider_category"] == "communication"
    assert suite["minimum_passing_scenarios"] == len(suite["scenarios"])
    assert [scenario["id"] for scenario in suite["scenarios"]] == [
        "catalog-contract-loads",
        "auth-boundary-is-declared",
        "provider-event-normalizes",
        "provider-delivery-replays-idempotently",
        "provider-write-is-policy-gated",
        "communication-loop-is-auditable",
    ]
    write_scenario = suite["scenarios"][4]
    assert write_scenario["mode"] == "policy_gate"
    assert "denied approval leaves provider state unchanged" in write_scenario[
        "expected_evidence"
    ]


def test_acceptance_suite_unknown_capability_returns_none():
    assert compile_integration_acceptance_suite("missing") is None


def test_integration_capability_route_exposes_acceptance_suite():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/acceptance-suite"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["provider_category"] == "developer"
    assert payload["scenarios"][-1]["id"] == "developer-change-is-review-safe"

    missing = client.get("/v1/integration-capabilities/not-real/acceptance-suite")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
