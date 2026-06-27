"""Integration capability recommendation compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_recommendations import (
    recommend_integration_capabilities,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_recommendations_rank_goal_relevant_capabilities():
    report = recommend_integration_capabilities(
        "Route failed CI and review comments into autonomous engineering repair tasks"
    )

    assert (
        report.goal
        == "Route failed CI and review comments into autonomous engineering repair tasks"
    )
    assert report.recommendations[0].slug == "github-engineering-copilot"
    assert report.recommendations[0].match_score > report.recommendations[1].match_score
    assert "developer" in report.recommendations[0].matched_signals
    assert "CI" in report.recommendations[0].rationale
    assert report.to_dict()["object"] == "integration_capability_recommendation_report"


def test_recommendations_can_filter_by_category_and_limit():
    report = recommend_integration_capabilities(
        "Create customer support triage agents that draft responses from tickets",
        category="crm_support",
        limit=1,
    )

    assert [entry.slug for entry in report.recommendations] == ["zendesk-intercom-support-desk"]
    assert report.category == "crm_support"
    assert report.limit == 1


def test_recommendation_route_exposes_goal_scored_catalog_matches():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/recommendations",
        params={"goal": "Import Notion docs into autonomous agent memory", "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "integration_capability_recommendation_report"
    assert payload["goal"] == "Import Notion docs into autonomous agent memory"
    assert payload["recommendations"][0]["slug"] == "notion-knowledge-operator"
    assert payload["recommendations"][0]["capability"]["category"] == "knowledge"


def test_recommendation_route_requires_non_empty_goal():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/recommendations", params={"goal": "   "})

    assert response.status_code == 422
