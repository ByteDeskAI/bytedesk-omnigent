"""Route tests for connected-app OAuth scope review."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.integration_scope_review import create_scope_review_router
from omnigent.errors import OmnigentError


class _NoIdentityAuth:
    def get_user_id(self, request: object) -> None:
        return None


def _app(auth_provider: object | None = None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(create_scope_review_router(auth_provider=auth_provider), prefix="/v1")
    return app


def test_scope_review_route_returns_policy_recommendations() -> None:
    client = TestClient(_app())

    res = client.post(
        "/v1/integration-scope-review",
        json={
            "service": "github",
            "requested_scopes": ["read:user", "repo"],
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "github"
    assert body["risk"] == "high"
    assert body["high_risk_scopes"] == ["repo"]
    assert body["requires_human_approval"] is True
    assert body["policy_recommendations"][0]["policy"] == "two_key_approval"


def test_scope_review_route_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)

    res = client.post(
        "/v1/integration-scope-review",
        json={"service": "slack", "requested_scopes": ["chat:write"]},
    )

    assert res.status_code == 401
