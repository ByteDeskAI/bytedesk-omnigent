"""Tests for deterministic integration agent blueprint previews."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_list_integration_agent_blueprints_exposes_ranked_services(
    client: httpx.AsyncClient,
) -> None:
    """The list endpoint advertises deterministic agent templates by service."""
    resp = await client.get("/v1/integration-agent-blueprints")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 8
    assert body["services"][0]["slug"] == "slack"
    assert body["services"][0]["agent_role"] == "Slack triage and response agent"
    assert body["services"][0]["auth_model"] == "oauth2"
    assert "chat:write" in body["services"][0]["recommended_scopes"]


@pytest.mark.asyncio
async def test_get_integration_agent_blueprint_returns_agent_creation_payload(
    client: httpx.AsyncClient,
) -> None:
    """A target-specific blueprint contains an agent spec and launch checklist."""
    resp = await client.get("/v1/integration-agent-blueprints/notion")

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"]["slug"] == "notion"
    assert body["agent_blueprint"]["suggested_name"] == "Notion Knowledge Steward"
    assert body["agent_blueprint"]["harness"] == "claude"
    assert body["agent_blueprint"]["capabilities"] == [
        "integration.notion.intake",
        "integration.notion.sync",
        "integration.notion.escalate",
    ]
    assert body["launch_checklist"] == [
        "Create or select the ByteDesk workspace that will own this connected app.",
        "Complete oauth2 authorization for Notion with the recommended scopes.",
        "Bind inbound Notion events to a ByteDesk task queue or agent inbox.",
        "Run a dry-run task against the agent before enabling autonomous writes.",
    ]


@pytest.mark.asyncio
async def test_get_unknown_integration_agent_blueprint_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """Unknown service slugs fail closed with the supported target list."""
    resp = await client.get("/v1/integration-agent-blueprints/unknown-crm")

    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["code"] == "integration_agent_blueprint_not_found"
    assert "slack" in body["detail"]["supported_slugs"]
