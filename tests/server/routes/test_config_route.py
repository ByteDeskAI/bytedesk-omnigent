"""Config-Control-Plane read API — integration + in-process smoke (BDP-2415)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.config import create_config_router
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
    app.include_router(create_config_router(auth_provider=auth_provider), prefix="/v1")
    return app


def _client() -> TestClient:
    # auth_provider=None → open (single-user/local), so reads succeed.
    return TestClient(_app(None), raise_server_exceptions=False)


def test_descriptors_catalog_is_self_describing() -> None:
    resp = _client().get("/v1/config/descriptors")
    assert resp.status_code == 200, resp.text
    by_key = {d["key"]: d for d in resp.json()["data"]}
    assert "system.log_level" in by_key
    nats = by_key["system.nats.url"]
    assert nats["tier"] == 0 and nats["writable"] is False
    assert nats["read_only_reason"]  # locked keys explain why
    assert "json_schema" in nats and "storage_source" in nats


def test_descriptors_filter_by_tier() -> None:
    resp = _client().get("/v1/config/descriptors", params={"tier": 0})
    assert resp.status_code == 200
    assert all(d["tier"] == 0 for d in resp.json()["data"])


def test_get_one_descriptor_and_404() -> None:
    client = _client()
    ok = client.get("/v1/config/descriptors/system.nats.url")
    assert ok.status_code == 200 and ok.json()["key"] == "system.nats.url"
    assert client.get("/v1/config/descriptors/nope.nope").status_code == 404


def test_get_value_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "WARNING")
    resp = _client().get("/v1/config/values/system.log_level")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == "WARNING"


def test_get_value_redacts_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATABASE_URI", "postgres://u:s3cr3t@h/db")
    resp = _client().get("/v1/config/values/system.database.uri")
    assert resp.status_code == 200
    body = resp.json()
    # NEVER the value — name + presence only, no leak.
    assert body["value"] == {"name": "system.database.uri", "present": True, "source": "env"}
    assert "s3cr3t" not in resp.text


def test_get_value_unknown_key_404() -> None:
    assert _client().get("/v1/config/values/nope.nope").status_code == 404


def test_read_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    assert client.get("/v1/config/descriptors").status_code == 401
    assert client.get("/v1/config/values/system.log_level").status_code == 401
