"""Integration onboarding questionnaire compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_onboarding_questionnaire import (
    compile_integration_onboarding_questionnaire,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_archon_onboarding_questionnaire_without_external_auth():
    questionnaire = compile_integration_onboarding_questionnaire(
        "archon-style-workflow-blueprints"
    )

    assert questionnaire is not None
    assert questionnaire["capability_slug"] == "archon-style-workflow-blueprints"
    assert questionnaire["auth_model"] == "Internal YAML/workflow schema"
    assert questionnaire["requires_external_auth"] is False
    assert questionnaire["sections"][0]["id"] == "workspace-intent"
    assert [section["id"] for section in questionnaire["sections"]][-1] == "workflow-harness"
    assert questionnaire["minimum_answer_count"] == sum(
        len(section["questions"]) for section in questionnaire["sections"]
    )
    assert all(
        "secret" not in question.lower()
        for section in questionnaire["sections"]
        for question in section["questions"]
    )


def test_compiles_provider_onboarding_questionnaire_with_scope_and_risk_prompts():
    questionnaire = compile_integration_onboarding_questionnaire("slack-command-center")

    assert questionnaire is not None
    assert questionnaire["requires_external_auth"] is True
    assert questionnaire["required_scopes"] == [
        "channels:history",
        "chat:write",
        "commands",
        "users:read",
    ]
    section_ids = [section["id"] for section in questionnaire["sections"]]
    assert section_ids == [
        "workspace-intent",
        "auth-boundary",
        "activation-policy",
        "communication-rollout",
    ]
    activation = questionnaire["sections"][2]
    assert "external_write" in activation["questions"][0]
    assert "approval" in " ".join(activation["questions"]).lower()


def test_unknown_capability_onboarding_questionnaire_returns_none():
    assert compile_integration_onboarding_questionnaire("missing") is None


def test_integration_capability_route_exposes_onboarding_questionnaire():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/notion-knowledge-operator/onboarding-questionnaire"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "notion-knowledge-operator"
    assert payload["sections"][-1]["id"] == "knowledge-scope"

    missing = client.get("/v1/integration-capabilities/not-real/onboarding-questionnaire")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
