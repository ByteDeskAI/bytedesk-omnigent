"""Tests for the integration webhook probe route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.integration_probe import create_integration_probe_router
from omnigent.errors import OmnigentError


class _NoIdentityAuth:
    """A multi-user auth provider that never resolves an identity → forces 401."""

    def get_user_id(self, request: object) -> None:
        return None


def _app(auth_provider: object | None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(
        create_integration_probe_router(auth_provider=auth_provider), prefix="/v1"
    )
    return app


def test_webhook_probe_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    resp = client.post(
        "/v1/integration-probes/webhook",
        json={
            "source": "github",
            "match_key": "issues.opened",
            "secret": "whsec_test",
            "payload": {"action": "opened"},
        },
    )
    assert resp.status_code == 401


def test_webhook_probe_route_returns_signed_probe_in_single_user_mode() -> None:
    client = TestClient(_app(None))
    resp = client.post(
        "/v1/integration-probes/webhook",
        json={
            "source": "github",
            "match_key": "issues.opened",
            "secret": "whsec_test",
            "payload": {"action": "opened"},
            "base_url": "https://omnigent.example.com/v1",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://omnigent.example.com/v1/ingress/github"
    assert body["body"] == '{"action":"opened"}'
    assert body["headers"]["x-omnigent-event"] == "issues.opened"
    assert "x-omnigent-signature" in body["headers"]
    assert body["expected_statuses"]["202"] == "binding exists and parked signal was delivered"
    assert body["curl_command"].startswith("curl -fsS -X POST")


def test_webhook_probe_route_rejects_ambiguous_body_sources() -> None:
    client = TestClient(_app(None), raise_server_exceptions=False)
    resp = client.post(
        "/v1/integration-probes/webhook",
        json={
            "source": "github",
            "match_key": "issues.opened",
            "secret": "whsec_test",
            "payload": {"action": "opened"},
            "raw_body": "{}",
        },
    )
    assert resp.status_code == 422
