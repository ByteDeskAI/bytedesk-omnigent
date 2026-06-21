"""Integration value scorecard tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_value_scorecards import (
    compile_integration_value_scorecard,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_value_scorecard_prioritizes_workflow_harness_business_impact():
    scorecard = compile_integration_value_scorecard("archon-style-workflow-blueprints")

    assert scorecard is not None
    assert scorecard["object"] == "integration_value_scorecard"
    assert scorecard["capability_slug"] == "archon-style-workflow-blueprints"
    assert scorecard["overall_score"] >= 95
    assert scorecard["dimensions"]["agent_autonomy"]["score"] == 100
    assert "deterministic" in " ".join(scorecard["recommended_sales_motion"]).lower()


def test_value_scorecard_marks_external_writes_as_enterprise_ready():
    scorecard = compile_integration_value_scorecard("linear-jira-work-intake")

    assert scorecard is not None
    assert scorecard["risk_tier"] == "external_write"
    assert scorecard["dimensions"]["buyer_pull"]["score"] >= 90
    assert any("approval" in item.lower() for item in scorecard["required_enablement"])


def test_value_scorecard_returns_none_for_unknown_slug():
    assert compile_integration_value_scorecard("missing") is None


def test_value_scorecard_api_exposes_detail_and_404s():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/slack-command-center/value-scorecard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "slack-command-center"
    assert payload["dimensions"]["time_to_value"]["score"] >= 80

    missing = client.get("/v1/integration-capabilities/not-real/value-scorecard")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
