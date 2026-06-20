"""Tests for deterministic third-party OAuth authorization URL compilation."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError


class _NoIdentityAuth:
    """A multi-user auth provider that never resolves an identity → forces 401."""

    def get_user_id(self, request: object) -> None:
        return None


def _route_app(auth_provider: object | None = None) -> FastAPI:
    from bytedesk_omnigent.routes.integration_authorization import (
        create_integration_authorization_router,
    )

    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(
        create_integration_authorization_router(auth_provider=auth_provider),
        prefix="/v1",
    )
    return app


def test_compile_slack_authorization_url_is_deterministic_and_stateful() -> None:
    from bytedesk_omnigent.integration_authorization import (
        compile_oauth_authorization_url,
    )

    result = compile_oauth_authorization_url(
        provider="slack",
        client_id="client_123",
        redirect_uri="https://omnigent.bytedesk.localhost/oauth/slack/callback",
        state="signed-state-token",
        scopes=["chat:write", "channels:read", "chat:write"],
    )

    parsed = urlparse(result.url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/oauth/v2/authorize"
    assert query == {
        "client_id": ["client_123"],
        "redirect_uri": ["https://omnigent.bytedesk.localhost/oauth/slack/callback"],
        "response_type": ["code"],
        "scope": ["chat:write,channels:read"],
        "state": ["signed-state-token"],
    }
    assert result.provider == "slack"
    assert result.scopes == ("chat:write", "channels:read")


def test_compile_github_authorization_url_uses_provider_defaults() -> None:
    from bytedesk_omnigent.integration_authorization import (
        compile_oauth_authorization_url,
    )

    result = compile_oauth_authorization_url(
        provider="github",
        client_id="gh_client",
        redirect_uri="https://example.test/callback",
        state="state-token",
    )

    query = parse_qs(urlparse(result.url).query)
    assert query["scope"] == ["repo read:org"]
    assert query["state"] == ["state-token"]


def test_compile_authorization_url_rejects_unknown_provider() -> None:
    from bytedesk_omnigent.integration_authorization import (
        UnknownOAuthProviderError,
        compile_oauth_authorization_url,
    )

    with pytest.raises(UnknownOAuthProviderError, match="unknown OAuth provider"):
        compile_oauth_authorization_url(
            provider="mystery-crm",
            client_id="client",
            redirect_uri="https://example.test/callback",
            state="state-token",
        )


def test_authorization_url_route_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_route_app(_NoIdentityAuth()), raise_server_exceptions=False)

    response = client.post(
        "/v1/integration-authorizations/authorize-url",
        json={
            "provider": "slack",
            "client_id": "client_123",
            "redirect_uri": "https://example.test/callback",
            "state": "state-token",
        },
    )

    assert response.status_code == 401


def test_authorization_url_route_returns_compiled_url_in_single_user_mode() -> None:
    client = TestClient(_route_app())

    response = client.post(
        "/v1/integration-authorizations/authorize-url",
        json={
            "provider": "linear",
            "client_id": "lin_client",
            "redirect_uri": "https://example.test/linear/callback",
            "state": "state-token",
            "scopes": ["read", "write", "read"],
            "extra_params": {"actor": "app"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "linear"
    assert body["scopes"] == ["read", "write"]
    parsed = urlparse(body["url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "linear.app"
    assert query["scope"] == ["read,write"]
    assert query["actor"] == ["app"]
