"""Integration capability bundle compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_capability_bundles import (
    compile_integration_capability_bundle,
    list_integration_capability_bundles,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_bundle_compiler_groups_catalog_entries_into_agent_workforce_offers():
    bundles = list_integration_capability_bundles()

    assert bundles[0].slug == "engineering-autonomy-stack"
    assert bundles[0].priority_score == 99
    assert bundles[0].capability_slugs == (
        "github-engineering-copilot",
        "linear-jira-work-intake",
        "slack-command-center",
    )
    assert "engineering" in bundles[0].business_case.lower()
    assert all(bundle.to_dict()["capabilities"] for bundle in bundles)


def test_bundle_detail_compiles_capabilities_and_activation_sequence():
    bundle = compile_integration_capability_bundle("customer-success-command-center")

    assert bundle is not None
    data = bundle.to_dict()
    assert data["object"] == "integration_capability_bundle"
    assert data["slug"] == "customer-success-command-center"
    assert [capability["slug"] for capability in data["capabilities"]] == [
        "zendesk-intercom-support-desk",
        "hubspot-salesforce-crm-agent",
        "notion-knowledge-operator",
    ]
    assert [phase["id"] for phase in data["activation_sequence"]] == [
        "catalog-confirmation",
        "auth-scope-review",
        "sandbox-dry-run",
        "pilot-with-approvals",
        "production-enable",
    ]
    assert data["aggregate_priority_score"] >= 90


def test_bundle_route_lists_and_reads_bundles_before_slug_route():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/bundles")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"][0]["slug"] == "engineering-autonomy-stack"

    detail = client.get("/v1/integration-capabilities/bundles/revenue-ops-agent-pack")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Revenue ops agent pack"

    missing = client.get("/v1/integration-capabilities/bundles/not-real")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
