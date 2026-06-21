"""Integration capability catalog tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_capabilities import (
    get_integration_capability,
    integration_capability_categories,
    list_integration_capabilities,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_catalog_prioritizes_archon_workflow_blueprint_first():
    entries = list_integration_capabilities()

    assert entries[0].slug == "archon-style-workflow-blueprints"
    assert entries[0].priority_score == 99
    assert "repeatable" in entries[0].business_case.lower()
    assert "https://github.com/coleam00/Archon" in entries[0].references


def test_catalog_entries_carry_required_strategy_fields():
    entries = list_integration_capabilities()

    assert len(entries) >= 10
    for entry in entries:
        data = entry.to_dict()
        assert data["implementation_description"]
        assert data["future_unlocks"]
        assert data["business_case"]
        assert data["agent_value"]
        assert isinstance(data["required_scopes"], list)


def test_catalog_filters_by_category():
    entries = list_integration_capabilities(category="project_management")

    assert {entry.slug for entry in entries} == {
        "linear-jira-work-intake",
        "trello-task-bridge",
    }


def test_catalog_lookup_and_categories():
    slack = get_integration_capability("slack-command-center")

    assert slack is not None
    assert slack.auth_model == "OAuth 2.0 bot + user tokens"
    assert "communication" in integration_capability_categories()
    assert get_integration_capability("missing") is None


def test_catalog_includes_teams_command_center_blueprint():
    teams = get_integration_capability("microsoft-teams-command-center")

    assert teams is not None
    assert teams.category == "communication"
    assert teams.auth_model == "Microsoft Graph OAuth 2.0 + bot framework"
    assert "ChannelMessage.Read.All" in teams.required_scopes
    assert "ChannelMessage.Send" in teams.required_scopes
    assert any("approvals" in value.lower() for value in teams.agent_value)
    assert any("ByteDesk" in value for value in teams.future_unlocks)


def test_integration_capabilities_router_lists_and_reads_entries():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities?limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert len(payload["data"]) == 2
    assert payload["data"][0]["slug"] == "archon-style-workflow-blueprints"
    assert "workflow_harness" in payload["categories"]

    detail = client.get("/v1/integration-capabilities/slack-command-center")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Slack command center"


def test_integration_capabilities_router_filters_and_404s():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities?category=communication")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [entry["slug"] for entry in data] == [
        "slack-command-center",
        "microsoft-teams-command-center",
    ]

    missing = client.get("/v1/integration-capabilities/not-real")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
