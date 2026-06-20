from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_secret_plans import compile_integration_secret_plan
from bytedesk_omnigent.routes.integration_secret_plans import (
    create_integration_secret_plans_router,
)


def test_compile_hubspot_secret_plan_includes_oauth_and_webhook_readiness() -> None:
    plan = compile_integration_secret_plan(
        {
            "provider": "HubSpot",
            "workspace_id": "acme-prod",
            "requested_events": ["contact.creation", "deal.propertyChange"],
            "writeback": True,
        }
    )

    assert plan.provider == "hubspot"
    assert plan.ingress_source == "hubspot"
    assert plan.workspace_id == "acme-prod"
    assert plan.required_secrets[0].env_var == "OMNIGENT_HUBSPOT_CLIENT_ID"
    assert "crm.objects.contacts.write" in plan.oauth_scopes
    assert "contact.creation" in plan.recommended_match_keys
    assert plan.provisioning_steps[0].id == "collect-oauth-app"
    assert plan.verification["secret_env_prefix"] == "OMNIGENT_HUBSPOT_"
    assert plan.idempotency_key == "integration-secret-plan:hubspot:acme-prod"


def test_compile_secret_plan_normalizes_aliases_and_denies_unknown_providers() -> None:
    plan = compile_integration_secret_plan(
        {"provider": "google-workspace", "workspace_id": "helms"}
    )

    assert plan.provider == "google_workspace"
    assert plan.ingress_source == "google-workspace"
    assert "https://www.googleapis.com/auth/calendar.readonly" in plan.oauth_scopes

    try:
        compile_integration_secret_plan({"provider": "unknown", "workspace_id": "helms"})
    except ValueError as exc:
        assert "unsupported provider" in str(exc)
    else:  # pragma: no cover - defensive clarity
        raise AssertionError("unknown providers must fail closed")


def test_integration_secret_plan_route_returns_compiled_payload() -> None:
    app = FastAPI()
    app.include_router(create_integration_secret_plans_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-secret-plans/compile",
        json={"provider": "zendesk", "workspace_id": "support", "writeback": False},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "zendesk"
    assert data["required_secrets"][0]["env_var"] == "OMNIGENT_ZENDESK_SUBDOMAIN"
    assert data["oauth_scopes"] == ["read"]
    assert data["approval_gates"] == ["install_connected_app"]


def test_integration_secret_plan_route_rejects_bad_request() -> None:
    app = FastAPI()
    app.include_router(create_integration_secret_plans_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-secret-plans/compile",
        json={"provider": "unsupported", "workspace_id": "support"},
    )

    assert response.status_code == 400
    assert "unsupported provider" in response.json()["detail"]
