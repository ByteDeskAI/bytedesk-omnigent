"""Integration agent prompt pack compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_agent_prompt_pack import (
    compile_integration_agent_prompt_pack,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_agent_prompt_pack():
    pack = compile_integration_agent_prompt_pack("archon-style-workflow-blueprints")

    assert pack is not None
    assert pack["object"] == "integration_agent_prompt_pack"
    assert pack["capability_slug"] == "archon-style-workflow-blueprints"
    assert (
        pack["agent_blueprint"]["role_name"]
        == "Archon-Style Workflow Blueprints Integration Agent"
    )
    assert pack["agent_blueprint"]["risk_tier"] == "internal_harness"
    assert pack["agent_blueprint"]["autonomy_mode"] == "deterministic_harness"
    assert pack["agent_blueprint"]["allowed_actions"] == [
        "compile workflow blueprints into Omnigent Tasks",
        "validate phase inputs, outputs, retry policy, and completion evidence",
        "emit operator-visible dry-run plans before execution",
    ]
    assert pack["agent_blueprint"]["blocked_actions"] == [
        "execute undeclared phases",
        "skip required verification evidence",
        "mutate external systems without an explicit connector capability",
    ]
    assert "workflow-determinism" in pack["verification_gate_ids"]
    assert "phase graph uses stable node ids" in pack["system_prompt"]


def test_compiles_external_write_prompt_pack_with_approval_boundaries():
    pack = compile_integration_agent_prompt_pack("slack-command-center")

    assert pack is not None
    assert pack["agent_blueprint"]["risk_tier"] == "external_write"
    assert pack["agent_blueprint"]["autonomy_mode"] == "approval_gated_write"
    assert "channels:history" in pack["agent_blueprint"]["required_scopes"]
    assert (
        "request human approval before provider-side writes"
        in pack["agent_blueprint"]["blocked_actions"]
    )
    assert "Human collaboration loop is bounded and auditable" in pack["system_prompt"]


def test_unknown_capability_prompt_pack_returns_none():
    assert compile_integration_agent_prompt_pack("missing") is None


def test_integration_capability_route_exposes_agent_prompt_pack():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/notion-knowledge-operator/agent-prompt-pack"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "notion-knowledge-operator"
    assert payload["agent_blueprint"]["autonomy_mode"] == "approval_gated_write"
    assert "knowledge-scope-control" in payload["verification_gate_ids"]

    missing = client.get("/v1/integration-capabilities/not-real/agent-prompt-pack")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
