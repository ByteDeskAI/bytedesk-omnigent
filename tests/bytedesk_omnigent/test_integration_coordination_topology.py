"""Integration coordination topology compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_coordination_topology import (
    compile_integration_coordination_topology,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_external_write_coordination_topology():
    topology = compile_integration_coordination_topology("slack-command-center")

    assert topology["object"] == "integration_coordination_topology"
    assert topology["capability_slug"] == "slack-command-center"
    assert topology["risk_tier"] == "external_write"
    assert [role["id"] for role in topology["agent_roles"]] == [
        "integration_orchestrator",
        "connector_operator",
        "policy_approver",
        "evidence_auditor",
    ]
    assert topology["agent_roles"][1]["approval_authority"] is False
    assert topology["agent_roles"][2]["approval_authority"] is True
    assert "chat:write" in topology["agent_roles"][1]["required_scopes"]
    assert topology["handoff_edges"] == [
        {
            "from": "integration_orchestrator",
            "to": "connector_operator",
            "trigger": "validated task requires provider context or action dispatch",
        },
        {
            "from": "connector_operator",
            "to": "policy_approver",
            "trigger": (
                "provider mutation, public message, record update, or broad data "
                "read requested"
            ),
        },
        {
            "from": "policy_approver",
            "to": "evidence_auditor",
            "trigger": "approved or denied action needs durable outcome evidence",
        },
    ]


def test_compiles_archon_workflow_coordination_topology():
    topology = compile_integration_coordination_topology(
        "archon-style-workflow-blueprints"
    )

    assert topology["risk_tier"] == "internal_harness"
    assert [role["id"] for role in topology["agent_roles"]] == [
        "workflow_orchestrator",
        "phase_executor",
        "verification_reviewer",
        "recovery_coordinator",
    ]
    assert topology["agent_roles"][0]["required_capabilities"] == [
        "workflow_harness.plan",
        "workflow_harness.route",
    ]
    assert "phase failed without terminal evidence" in topology["escalation_triggers"]


def test_unknown_capability_coordination_topology_returns_none():
    assert compile_integration_coordination_topology("missing") is None


def test_integration_capability_route_exposes_coordination_topology():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/coordination-topology"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["agent_roles"][1]["id"] == "connector_operator"
    assert "repository permission expansion requested" in payload["escalation_triggers"]

    missing = client.get(
        "/v1/integration-capabilities/not-real/coordination-topology"
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
