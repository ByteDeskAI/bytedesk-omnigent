"""Integration evidence packet compiler tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_evidence_packet import (
    compile_integration_evidence_packet,
)
from bytedesk_omnigent.routes.integration_capabilities import (
    create_integration_capabilities_router,
)


def test_compiles_provider_evidence_packet_from_verification_matrix():
    packet = compile_integration_evidence_packet("slack-command-center")

    assert packet is not None
    assert packet["object"] == "integration_evidence_packet"
    assert packet["capability_slug"] == "slack-command-center"
    assert packet["risk_tier"] == "external_write"
    assert packet["operator_summary"] == (
        "Collect 24 required evidence item(s) across 8 gate(s) before enabling "
        "Slack command center for production tenants."
    )
    assert packet["review_lane"] == "security_and_operations"
    assert packet["evidence_items"][0] == {
        "id": "catalog-contract:1",
        "gate_id": "catalog-contract",
        "gate_title": "Catalog contract is explicit and stable",
        "required_evidence": "capability slug resolves in the integration catalog",
        "status": "required",
    }
    assert packet["collection_notes"] == [
        "Treat provider tokens, signing secrets, and customer payload samples as "
        "redacted evidence only.",
        "Capture tenant/workspace identifiers as opaque ids; do not include raw "
        "customer content in the packet.",
        "External write integrations require approval evidence before any provider-side "
        "mutation is enabled.",
    ]
    assert packet["handoff_prompt"].startswith(
        "Verify Slack command center readiness by attaching evidence for"
    )


def test_compiles_internal_workflow_packet_without_provider_secret_notes():
    packet = compile_integration_evidence_packet("archon-style-workflow-blueprints")

    assert packet is not None
    assert packet["risk_tier"] == "internal_harness"
    assert packet["review_lane"] == "platform_architecture"
    assert packet["collection_notes"] == [
        "Attach deterministic fixture inputs, expected phase outputs, and replay logs "
        "for each workflow gate.",
        "Include schema version and migration notes when workflow blueprint contracts change.",
    ]


def test_unknown_capability_evidence_packet_returns_none():
    assert compile_integration_evidence_packet("missing") is None


def test_integration_capability_route_exposes_evidence_packet():
    app = FastAPI()
    app.include_router(create_integration_capabilities_router(), prefix="/v1")
    client = TestClient(app)

    response = client.get("/v1/integration-capabilities/notion-knowledge-operator/evidence-packet")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_slug"] == "notion-knowledge-operator"
    assert payload["review_lane"] == "data_governance"
    assert payload["evidence_items"][-1]["gate_id"] == "knowledge-scope-control"

    missing = client.get("/v1/integration-capabilities/not-real/evidence-packet")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
