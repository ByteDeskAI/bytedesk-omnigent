"""Integration tool contract compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_tool_contracts import (
    compile_integration_tool_contract,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_slack_tool_contract_with_policy_bound_operations():
    contract = compile_integration_tool_contract("slack-command-center")

    assert contract is not None
    assert contract["capability_slug"] == "slack-command-center"
    assert contract["risk_tier"] == "external_write"
    assert [tool["name"] for tool in contract["tools"]] == [
        "slack_command_center.read_context",
        "slack_command_center.normalize_event",
        "slack_command_center.execute_action",
        "slack_command_center.record_evidence",
    ]
    write_tool = contract["tools"][2]
    assert write_tool["approval_required"] is True
    assert write_tool["required_scopes"] == ["chat:write", "commands"]
    assert "dry_run" in write_tool["required_inputs"]
    assert contract["agent_blueprint_hints"] == [
        "Grant read-context tools to intake and triage agents first.",
        "Reserve execute-action tools for agents with explicit policy gates.",
        "Bind every provider mutation to outcome evidence before completing the task.",
    ]


def test_compiles_archon_workflow_contract_without_external_write_tool():
    contract = compile_integration_tool_contract("archon-style-workflow-blueprints")

    assert contract is not None
    assert contract["risk_tier"] == "internal_harness"
    assert [tool["name"] for tool in contract["tools"]] == [
        "archon_style_workflow_blueprints.read_context",
        "archon_style_workflow_blueprints.normalize_event",
        "archon_style_workflow_blueprints.record_evidence",
    ]
    assert all(tool["approval_required"] is False for tool in contract["tools"])
    assert contract["tools"][0]["required_scopes"] == []


def test_unknown_capability_tool_contract_returns_none():
    assert compile_integration_tool_contract("missing") is None


def test_integration_capability_route_exposes_tool_contract():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/github-engineering-copilot/tool-contract")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["tools"][2]["category_policy_gate"] == "developer-change-safety"

    missing = client.get("/v1/integration-capabilities/not-real/tool-contract")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
