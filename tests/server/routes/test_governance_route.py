"""Tests for the governance route auth gate + bounded leaderboard limit (BDP-2289).

The 401 (require_user) and 422 (param validation) paths both fire before the
handler reaches the durable stores, so these are self-contained — no runtime/store
init needed.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.governance import create_governance_router
from omnigent.errors import OmnigentError


class _NoIdentityAuth:
    """A multi-user auth provider that never resolves an identity → forces 401."""

    def get_user_id(self, request: object) -> None:
        return None


def _app(auth_provider: object) -> FastAPI:
    app = FastAPI()
    # Mirror the main app's OmnigentError → http_status mapping (app.py:1155) so
    # require_user's UNAUTHORIZED surfaces as a real 401.
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(create_governance_router(auth_provider=auth_provider), prefix="/v1")
    return app


def test_summary_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    assert client.get("/v1/governance/summary").status_code == 401


def test_leaderboard_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    # Valid params → reaches the handler → require_user rejects the unauthenticated caller.
    assert client.get("/v1/governance/leaderboard?metric=revenue").status_code == 401


def test_leaderboard_limit_is_bounded() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    # Param validation runs BEFORE the handler/auth → 422 for an out-of-range limit
    # (was unbounded: SQLite LIMIT -1 dumped the whole scoreboard).
    assert client.get("/v1/governance/leaderboard?metric=revenue&limit=-1").status_code == 422
    assert client.get("/v1/governance/leaderboard?metric=revenue&limit=0").status_code == 422
    assert client.get("/v1/governance/leaderboard?metric=revenue&limit=1000").status_code == 422
