"""Tests for connected-app installation manifest compilation."""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.connected_app_manifests import (
    compile_connected_app_manifest,
    provider_slugs,
)
from bytedesk_omnigent.extension import BytedeskExtension
from omnigent.extensions import OmnigentExtension, install_extensions


def test_compile_github_manifest_is_deterministic_and_writeback_gated() -> None:
    first = compile_connected_app_manifest(
        provider="GitHub",
        workspace_id="Acme_Repo",
        public_base_url="https://omnigent.bytedesk.localhost/",
        desired_capabilities=["code_review.pull_request"],
        tenant_id="tenant_1",
        writeback_enabled=True,
    )
    second = compile_connected_app_manifest(
        provider="github",
        workspace_id="acme-repo",
        public_base_url="https://omnigent.bytedesk.localhost",
        desired_capabilities=["code_review.pull_request"],
        tenant_id="tenant_1",
        writeback_enabled=True,
    )

    assert first == second
    assert first.provider == "github"
    assert first.auth_model == "github_app"
    assert first.ingress_path == "/v1/ingress/github-acme-repo"
    assert first.secret_env_var == "OMNIGENT_INGRESS_SECRET_GITHUB_ACME_REPO"
    assert first.redirect_uri == (
        "https://omnigent.bytedesk.localhost/v1/connected-apps/oauth/github/callback"
    )
    assert "pull_requests:write" in first.required_scopes
    assert first.task_defaults["required_capability"] == "developer.work_item"
    assert first.task_defaults["desired_capabilities"] == [
        "developer.work_item",
        "code_review.pull_request",
    ]
    assert first.bytedesk_mount["ingress_url"].endswith(
        "/v1/ingress/github-acme-repo"
    )
    assert [gate["gate"] for gate in first.approval_gates] == [
        "approval.required_before_autonomous_execution",
        "approval.required_before_repository_write",
    ]


def test_compile_slack_manifest_without_writeback_omits_write_scope() -> None:
    manifest = compile_connected_app_manifest(
        provider="slack",
        workspace_id="helms-ai",
        public_base_url="https://agents.example.com",
        writeback_enabled=False,
    )

    assert "chat:write" in manifest.required_scopes  # needed for agent responses
    assert "reactions:write" not in manifest.required_scopes
    assert manifest.task_defaults["required_capability"] == "team_chat.agent_request"
    assert manifest.approval_gates == [
        {
            "gate": "approval.required_before_autonomous_execution",
            "reason": "connected app events can trigger hosted Omnigent agents",
        }
    ]


def test_compile_rejects_unknown_provider_and_bad_base_url() -> None:
    try:
        compile_connected_app_manifest(
            provider="unknown",
            workspace_id="acme",
            public_base_url="https://agents.example.com",
        )
    except ValueError as exc:
        assert "unsupported provider" in str(exc)
        assert "github" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unsupported provider was accepted")

    try:
        compile_connected_app_manifest(
            provider="github",
            workspace_id="acme",
            public_base_url="not-a-url",
        )
    except ValueError as exc:
        assert "absolute http(s) URL" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("bad base URL was accepted")


def test_extension_route_compiles_manifest() -> None:
    app = FastAPI()
    install_extensions(app, extensions=[cast(OmnigentExtension, BytedeskExtension())])

    response = TestClient(app).post(
        "/v1/connected-app-manifests/compile",
        json={
            "provider": "linear",
            "workspace_id": "product-team",
            "public_base_url": "https://omnigent.example.com",
            "writeback_enabled": True,
        },
    )

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["provider"] == "linear"
    assert manifest["ingress_source"] == "linear-product-team"
    assert manifest["task_defaults"]["required_capability"] == (
        "project_management.work_item"
    )
    assert "comments:write" in manifest["required_scopes"]


def test_extension_route_returns_supported_providers_on_bad_input() -> None:
    app = FastAPI()
    install_extensions(app, extensions=[cast(OmnigentExtension, BytedeskExtension())])

    response = TestClient(app).post(
        "/v1/connected-app-manifests/compile",
        json={
            "provider": "jira",
            "workspace_id": "product-team",
            "public_base_url": "https://omnigent.example.com",
        },
    )

    assert response.status_code == 400
    assert set(response.json()["supported_providers"]) == set(provider_slugs())
