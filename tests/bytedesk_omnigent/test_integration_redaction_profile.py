"""Integration redaction profile compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_redaction_profile import (
    compile_integration_redaction_profile,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_secret_free_external_write_redaction_profile():
    profile = compile_integration_redaction_profile("slack-command-center")

    assert profile is not None
    assert profile["object"] == "integration_redaction_profile"
    assert profile["capability_slug"] == "slack-command-center"
    assert profile["risk_tier"] == "external_write"
    assert profile["default_log_level"] == "metadata_only"
    assert "authorization" in profile["always_redact_headers"]
    assert "chat:write" in profile["sensitive_scopes"]
    assert profile["field_rules"][-1] == {
        "field": "message.text",
        "action": "summarize",
        "reason": "communication payloads can contain customer, employee, or approval context",
    }
    assert any(
        rule["field"] == "outbound_request.body" and rule["action"] == "redact"
        for rule in profile["field_rules"]
    )


def test_compiles_internal_workflow_profile_without_provider_scope_rules():
    profile = compile_integration_redaction_profile("archon-style-workflow-blueprints")

    assert profile is not None
    assert profile["risk_tier"] == "internal_harness"
    assert profile["default_log_level"] == "structured_evidence"
    assert profile["sensitive_scopes"] == []
    assert profile["retention_policy"] == {
        "evidence_days": 30,
        "payload_days": 0,
        "rationale": (
            "internal workflow harnesses should keep structured evidence "
            "without retaining raw phase payloads"
        ),
    }
    assert profile["field_rules"][-1] == {
        "field": "workflow.phase.output",
        "action": "hash",
        "reason": (
            "phase outputs may include generated customer artifacts; hashes "
            "preserve deterministic replay evidence"
        ),
    }


def test_unknown_capability_redaction_profile_returns_none():
    assert compile_integration_redaction_profile("not-real") is None


def test_integration_capability_route_exposes_redaction_profile():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get(
        "/v1/integration-capabilities/github-engineering-copilot/redaction-profile"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "github-engineering-copilot"
    assert payload["category"] == "developer"
    assert payload["field_rules"][-1]["field"] == "repository.diff"

    missing = client.get("/v1/integration-capabilities/not-real/redaction-profile")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
