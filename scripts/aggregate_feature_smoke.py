#!/usr/bin/env python3
"""Smoke-test the aggregate Omnigent integration feature branch.

This script is intentionally lightweight: it uses FastAPI's TestClient against
ByteDesk extension routers, so Ryan can verify the aggregated integration
surfaces without booting the full Omnigent server, MicroK8s, or external SaaS
credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.extension import BytedeskExtension
from bytedesk_omnigent.ingress import (
    GitHubWebhookAdapter,
    resolve_webhook_adapter,
    verify_hmac_signature,
)


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    run: Callable[[TestClient], dict[str, Any]]


def _app() -> FastAPI:
    app = FastAPI(title="ByteDesk aggregate smoke harness")
    for router in BytedeskExtension().routers(auth_provider=None):
        app.include_router(router, prefix="/v1")
    return app


def _get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, f"GET {path} -> {response.status_code}: {response.text}"
    return response.json()


def _post(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    assert response.status_code == 200, f"POST {path} -> {response.status_code}: {response.text}"
    return response.json()


def check_extension_health(client: TestClient) -> dict[str, Any]:
    payload = _get(client, "/v1/_ext/health")
    assert payload == {"extension": "bytedesk", "loaded": True}
    return payload


def check_capability_catalog(client: TestClient) -> dict[str, Any]:
    payload = _get(client, "/v1/integration-capabilities?limit=3")
    slugs = [entry["slug"] for entry in payload["data"]]
    assert slugs[0] == "archon-style-workflow-blueprints"
    assert "workflow_harness" in payload["categories"]
    return {"top_slugs": slugs, "categories": payload["categories"][:5]}


def check_static_route_ordering(client: TestClient) -> dict[str, Any]:
    bundles = _get(client, "/v1/integration-capabilities/bundles")
    recommendations = _get(
        client,
        "/v1/integration-capabilities/recommendations"
        "?goal=Import%20Notion%20docs%20into%20agent%20memory&limit=2",
    )
    assert bundles["object"] == "list"
    assert len(bundles["data"]) >= 1
    assert recommendations["object"] == "integration_capability_recommendation_report"
    assert len(recommendations["recommendations"]) == 2
    return {
        "bundle_count": len(bundles["data"]),
        "recommendations": [
            item["slug"] for item in recommendations["recommendations"]
        ],
    }


def check_capability_artifacts(client: TestClient) -> dict[str, Any]:
    slug = "slack-command-center"
    paths = {
        "marketplace": f"/v1/integration-capabilities/{slug}/marketplace-listing",
        "verification": f"/v1/integration-capabilities/{slug}/verification-matrix",
        "launch": f"/v1/integration-capabilities/{slug}/launch-brief",
        "lifecycle": f"/v1/integration-capabilities/{slug}/lifecycle-plan",
        "tool_contract": f"/v1/integration-capabilities/{slug}/tool-contract",
    }
    payloads = {name: _get(client, path) for name, path in paths.items()}
    assert payloads["marketplace"]["capability_slug"] == slug
    assert payloads["marketplace"]["package_type"] == "integration_capability"
    assert payloads["lifecycle"]["capability_slug"] == slug
    return {
        "artifact_names": sorted(payloads),
        "marketplace_summary": payloads["marketplace"]["summary"],
        "lifecycle_stages": [stage["id"] for stage in payloads["lifecycle"]["stages"]],
    }


def check_readiness_and_evidence(client: TestClient) -> dict[str, Any]:
    readiness = _post(
        client,
        "/v1/integration-capabilities/google-workspace-operator/readiness-assessment",
        {
            "evidence": {
                "catalog-contract": [
                    "capability slug resolves in the integration catalog",
                    "auth model and required scopes are documented",
                    "business case and future unlocks are present",
                ]
            }
        },
    )
    evidence = _post(
        client,
        "/v1/integration-capabilities/google-workspace-operator/evidence-assessment",
        {
            "evidence_items": [
                {
                    "gate_id": "knowledge-scope-control",
                    "evidence": ["read set is constrained to selected files, pages, or databases"],
                    "source": "aggregate-smoke",
                }
            ]
        },
    )
    assert readiness["capability_slug"] == "google-workspace-operator"
    assert readiness["gates"][0]["status"] == "satisfied"
    assert evidence["gate_results"][-1]["gate_id"] == "knowledge-scope-control"
    return {
        "readiness_state": readiness["activation_state"],
        "satisfied_gate_count": readiness["satisfied_gate_count"],
        "evidence_ready": evidence["ready_for_activation"],
    }


def check_ingress_adapters(client: TestClient) -> dict[str, Any]:
    manifest = _get(client, "/v1/ingress/adapters")
    sources = {entry["source"] for entry in manifest["adapters"]}
    expected = {"slack", "stripe", "github", "google-workspace", "microsoft-teams", "salesforce"}
    assert expected.issubset(sources), sorted(expected - sources)

    body = b'{"action":"opened"}'
    secret = "aggregate-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_signature(body, secret, "sha256=" + signature)
    adapter = resolve_webhook_adapter("github")
    assert isinstance(adapter, GitHubWebhookAdapter)
    assert adapter.match_key({"x-github-event": "pull_request"}) == "pull_request"
    return {"adapter_count": len(sources), "sample_sources": sorted(sources)[:10]}


def check_webhook_probe(client: TestClient) -> dict[str, Any]:
    payload = _post(
        client,
        "/v1/integration-probes/webhook",
        {
            "source": "github",
            "match_key": "pull_request",
            "secret": "aggregate-secret",
            "payload": {"action": "opened", "number": 194},
            "base_url": "http://omnigent.bytedesk.localhost/v1",
        },
    )
    assert payload["url"].endswith("/v1/ingress/github")
    assert "curl" in payload["curl_command"]
    assert payload["headers"]
    return {
        "url": payload["url"],
        "expected_statuses": payload["expected_statuses"],
        "curl_preview": payload["curl_command"][:140] + "...",
    }


def check_approval_plan(client: TestClient) -> dict[str, Any]:
    payload = _post(
        client,
        "/v1/integration-approval-plans/compile",
        {
            "provider": "slack",
            "scopes": ["channels:history", "chat:write", "commands"],
            "requested_operations": ["read_channel", "post_message"],
            "writeback_enabled": True,
        },
    )
    plan = payload["approval_plan"]
    assert plan["provider"] == "slack"
    assert "autonomous_writeback_requested" in plan["reasons"]
    return {
        "provider": plan["provider"],
        "risk_level": plan["risk_level"],
        "required_approval": plan["required_approval"],
        "gates": plan["gates"],
    }


CHECKS: tuple[SmokeCheck, ...] = (
    SmokeCheck("extension health", check_extension_health),
    SmokeCheck("capability catalog", check_capability_catalog),
    SmokeCheck("static route ordering", check_static_route_ordering),
    SmokeCheck("capability artifacts", check_capability_artifacts),
    SmokeCheck("readiness/evidence assessment", check_readiness_and_evidence),
    SmokeCheck("ingress adapters", check_ingress_adapters),
    SmokeCheck("webhook probe", check_webhook_probe),
    SmokeCheck("approval plan", check_approval_plan),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    client = TestClient(_app())
    results: list[dict[str, Any]] = []
    for check in CHECKS:
        detail = check.run(client)
        results.append({"name": check.name, "status": "ok", "detail": detail})
        if not args.json:
            print(f"PASS {check.name}")
            print(json.dumps(detail, indent=2, sort_keys=True))

    if args.json:
        print(json.dumps({"status": "ok", "checks": results}, indent=2, sort_keys=True))
    else:
        print(f"\naggregate smoke passed: {len(results)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
