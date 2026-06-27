"""Integration consent manifest compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_consent_manifest import (
    compile_integration_consent_manifest,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_google_workspace_consent_manifest_with_scope_rationales():
    manifest = compile_integration_consent_manifest("google-workspace-operator")
    expected_rationale = (
        "Allows Omnigent agents to perform the cataloged Google Workspace operator "
        "workflow with least-privilege access."
    )

    assert manifest is not None
    assert manifest["object"] == "integration_consent_manifest"
    assert manifest["capability_slug"] == "google-workspace-operator"
    assert manifest["provider_category"] == "knowledge"
    assert manifest["consent_summary"].startswith("Connect Google Workspace")
    assert manifest["operator_disclosure"]
    assert manifest["risk_prompts"] == [
        "Confirm selected files, pages, databases, or mailboxes before granting "
        "broad read access.",
        "Require explicit approval before an agent sends email or shares generated "
        "documents externally.",
    ]
    assert manifest["scope_rationales"] == [
        {
            "scope": "https://www.googleapis.com/auth/drive.file",
            "rationale": expected_rationale,
            "risk_level": "moderate",
        },
        {
            "scope": "https://www.googleapis.com/auth/documents",
            "rationale": expected_rationale,
            "risk_level": "moderate",
        },
        {
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "rationale": expected_rationale,
            "risk_level": "moderate",
        },
        {
            "scope": "https://www.googleapis.com/auth/calendar.events",
            "rationale": expected_rationale,
            "risk_level": "moderate",
        },
    ]


def test_internal_workflow_harness_manifest_is_credentialless():
    manifest = compile_integration_consent_manifest("archon-style-workflow-blueprints")

    assert manifest is not None
    assert manifest["provider_category"] == "workflow_harness"
    assert manifest["consent_summary"] == (
        "Enable Archon-style deterministic workflow blueprints without external OAuth credentials."
    )
    assert manifest["scope_rationales"] == []
    assert manifest["operator_disclosure"] == (
        "This capability is internal to Omnigent and does not request third-party account access."
    )
    assert manifest["risk_prompts"] == [
        "Review workflow phase inputs, outputs, and completion evidence before activation."
    ]


def test_unknown_capability_returns_none():
    assert compile_integration_consent_manifest("missing") is None


def test_integration_capability_route_exposes_consent_manifest():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/slack-command-center/consent-manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "slack-command-center"
    assert payload["provider_category"] == "communication"
    assert payload["risk_prompts"] == [
        "Confirm which channels, teams, or threads agents may read before enabling ingestion.",
        "Require approval before agents post messages visible to humans or customers.",
    ]

    missing = client.get("/v1/integration-capabilities/not-real/consent-manifest")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
