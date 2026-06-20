"""Integration dependency graph compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_dependency_graph import (
    compile_integration_dependency_graph,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_workflow_dependency_graph():
    graph = compile_integration_dependency_graph("archon-style-workflow-blueprints")

    assert graph is not None
    assert graph["capability_slug"] == "archon-style-workflow-blueprints"
    assert graph["category"] == "workflow_harness"
    assert graph["object"] == "integration_dependency_graph"
    assert graph["recommended_sequence"] == [
        "catalog-contract",
        "workflow-schema",
        "phase-compiler",
        "verification-harness",
        "operator-observability",
    ]
    assert graph["nodes"][0] == {
        "id": "catalog-contract",
        "title": "Catalog contract and rollout intent",
        "depends_on": [],
        "deliverables": [
            "resolved catalog entry with business case",
            "auth model and required scopes reviewed",
            "owner-visible future unlocks documented",
        ],
    }
    harness_node = graph["nodes"][1]
    assert harness_node["id"] == "workflow-schema"
    assert harness_node["depends_on"] == ["catalog-contract"]
    assert "typed phase input/output contract" in harness_node["deliverables"]


def test_compiles_provider_specific_dependency_graph():
    graph = compile_integration_dependency_graph("linear-jira-work-intake")

    assert graph is not None
    assert graph["capability_slug"] == "linear-jira-work-intake"
    assert graph["category"] == "project_management"
    assert graph["recommended_sequence"] == [
        "catalog-contract",
        "auth-sandbox",
        "webhook-ingress",
        "work-item-mapping",
        "policy-and-idempotency",
        "operator-observability",
    ]
    assert graph["nodes"][1]["id"] == "auth-sandbox"
    assert any(
        "OAuth 2.0 / Atlassian 3LO" in item
        for item in graph["nodes"][1]["deliverables"]
    )
    assert graph["nodes"][3] == {
        "id": "work-item-mapping",
        "title": "Work tracker lifecycle mapping",
        "depends_on": ["webhook-ingress"],
        "deliverables": [
            "external issue/card states mapped to Omnigent Task lifecycle",
            "comment, checklist, and assignee attribution preserved",
            "status write-back conflict policy documented",
        ],
    }


def test_unknown_capability_dependency_graph_returns_none():
    assert compile_integration_dependency_graph("missing") is None


def test_integration_capability_route_exposes_dependency_graph():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/dependency-graph"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["nodes"][3]["id"] == "developer-change-safety"

    missing = client.get("/v1/integration-capabilities/not-real/dependency-graph")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
