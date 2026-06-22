"""Integration demo scenario compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_demo_scenarios import (
    compile_integration_demo_scenario,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compile_demo_scenario_for_archon_blueprint():
    scenario = compile_integration_demo_scenario("archon-style-workflow-blueprints")
    payload = scenario.to_dict()

    assert payload["capability_slug"] == "archon-style-workflow-blueprints"
    assert payload["scenario_slug"] == "demo-archon-style-workflow-blueprints"
    assert payload["entrypoint"] == "Internal workflow blueprint run"
    assert payload["agent_roles"] == [
        "workflow-designer",
        "specialist-agent",
        "verification-agent",
    ]
    assert payload["demo_steps"] == [
        "Select a repeatable customer workflow and capture its typed inputs.",
        "Compile phases into Omnigent tasks with explicit owners, retry policy, "
        "and evidence requirements.",
        "Run the workflow in dry-run mode and collect completion evidence from every phase.",
        "Promote the verified blueprint into a reusable ByteDesk Platform template.",
    ]
    assert "blueprint" in payload["success_metrics"][0].lower()


def test_compile_demo_scenario_for_external_slack_capability():
    scenario = compile_integration_demo_scenario("slack-command-center")
    payload = scenario.to_dict()

    assert payload["entrypoint"] == "Slack event or command"
    assert payload["agent_roles"] == [
        "integration-concierge",
        "domain-specialist-agent",
        "human-approval-reviewer",
    ]
    assert payload["sample_trigger"] == "A Slack command center event requests agent assistance."
    assert payload["demo_steps"][0].startswith("Receive and normalize")
    assert payload["demo_steps"][-1].startswith("Publish the final outcome")


def test_compile_demo_scenario_unknown_slug_returns_none():
    assert compile_integration_demo_scenario("not-real") is None


def test_demo_scenario_router_endpoint():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/slack-command-center/demo-scenario")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "slack-command-center"
    assert payload["scenario_slug"] == "demo-slack-command-center"
    assert "business_case" in payload


def test_demo_scenario_router_404s_for_unknown_capability():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/not-real/demo-scenario")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
