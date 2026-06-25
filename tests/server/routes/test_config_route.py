"""Config-Control-Plane read API — integration + in-process smoke (BDP-2415)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.config import create_config_router
from omnigent.config import ConfigChange, config_change_bus
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


def test_descriptors_filter_by_scope() -> None:
    resp = _client().get("/v1/config/descriptors", params={"scope": "system"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data
    assert all(d["scope"] == "system" for d in data)


def test_descriptors_filter_by_writable() -> None:
    resp = _client().get("/v1/config/descriptors", params={"writable": "false"})
    assert resp.status_code == 200
    assert all(d["writable"] is False for d in resp.json()["data"])


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


# ── writes (BDP-2417) ─────────────────────────────────────────────────────────

_MODEL = "/v1/config/values/system.default_ad_hoc_model"


def test_put_value_succeeds_with_if_match() -> None:
    client = _client()
    cur = client.get(_MODEL).json()
    resp = client.put(
        _MODEL,
        json={"value": "claude-opus-4-8"},
        headers={"If-Match": f'"{cur["etag"]}"'},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == "claude-opus-4-8"
    assert client.get(_MODEL).json()["value"] == "claude-opus-4-8"  # live


def test_put_stale_if_match_412() -> None:
    client = _client()
    cur = client.get(_MODEL).json()
    client.put(_MODEL, json={"value": "a"}, headers={"If-Match": f'"{cur["etag"]}"'})
    stale = client.put(_MODEL, json={"value": "b"}, headers={"If-Match": f'"{cur["etag"]}"'})
    assert stale.status_code == 412, stale.text


def test_put_tier0_is_409() -> None:
    resp = _client().put("/v1/config/values/system.nats.url", json={"value": "nats://x"})
    assert resp.status_code == 409, resp.text


def test_put_floor_violation_400() -> None:
    resp = _client().put(
        "/v1/config/values/policies.cost_hard_stop.default_ceiling_usd",
        json={"value": 0},
    )
    assert resp.status_code == 400, resp.text


def test_put_schema_violation_400() -> None:
    resp = _client().put(_MODEL, json={"value": 123})  # expects a string
    assert resp.status_code == 400, resp.text


def test_put_unknown_404() -> None:
    assert _client().put("/v1/config/values/nope.nope", json={"value": "x"}).status_code == 404


def test_put_requires_auth() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    assert client.put(_MODEL, json={"value": "x"}).status_code == 401


def test_put_rejects_body_without_value_key() -> None:
    resp = _client().put(_MODEL, json={"nope": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_input"


def test_put_rejects_non_object_body() -> None:
    client = _client()
    resp = client.put(
        _MODEL, content=b'"just-a-string"', headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_input"


# ── realtime SSE (BDP-2418) ───────────────────────────────────────────────────


async def test_sse_streams_config_changed_metadata_only() -> None:
    """GET /config/events emits config.changed carrying metadata only, no value.

    Driven at the ASGI protocol level (one event loop, controlled receive/send)
    so the infinite stream terminates deterministically instead of buffering —
    httpx ASGITransport collects the whole body, which would hang on a stream.
    """
    app = _app(None)
    start: dict[str, object] = {}
    body_chunks: list[bytes] = []
    headers_sent = asyncio.Event()
    disconnect = asyncio.Event()

    async def receive() -> dict[str, object]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
            headers_sent.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"") or b""
            if chunk:
                body_chunks.append(chunk)
                disconnect.set()  # one real chunk is enough — let the loop exit

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/config/events",
        "raw_path": b"/v1/config/events",
        "headers": [],
        "query_string": b"",
        "client": ("test", 0),
        "server": ("test", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(headers_sent.wait(), timeout=5)  # endpoint subscribed
    assert start["status"] == 200
    content_type = dict(start["headers"])[b"content-type"]  # type: ignore[arg-type]
    assert b"text/event-stream" in content_type

    config_change_bus().publish(
        ConfigChange(
            key="system.default_ad_hoc_model",
            scope="system",
            etag="7",
            tier=2,
            effect_timing="live",
        )
    )
    await asyncio.wait_for(task, timeout=5)

    body = b"".join(body_chunks).decode()
    assert "event: config.changed" in body
    payload = json.loads(body.split("data:", 1)[1].split("\n\n", 1)[0].strip())
    assert payload == {
        "key": "system.default_ad_hoc_model",
        "scope": "system",
        "etag": "7",
        "tier": 2,
        "effect_timing": "live",
    }
    assert "value" not in payload


async def test_sse_requires_auth() -> None:
    app = _app(_NoIdentityAuth())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/v1/config/events")
        assert resp.status_code == 401


async def test_sse_emits_heartbeat_on_queue_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle SSE connections yield a comment heartbeat when no event arrives."""
    app = _app(None)
    start: dict[str, object] = {}
    body_chunks: list[bytes] = []
    headers_sent = asyncio.Event()
    disconnect = asyncio.Event()
    orig_wait_for = asyncio.wait_for

    async def _timeout_queue_get_only(coro, timeout=None):
        code_name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        if code_name == "get":
            if hasattr(coro, "close"):
                coro.close()
            raise asyncio.TimeoutError()
        return await orig_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.config.asyncio.wait_for", _timeout_queue_get_only
    )

    async def receive() -> dict[str, object]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
            headers_sent.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"") or b""
            if chunk:
                body_chunks.append(chunk)
                disconnect.set()

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/config/events",
        "raw_path": b"/v1/config/events",
        "headers": [],
        "query_string": b"",
        "client": ("test", 0),
        "server": ("test", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(headers_sent.wait(), timeout=5)
    await asyncio.wait_for(task, timeout=5)

    body = b"".join(body_chunks).decode()
    assert ": heartbeat" in body
    _ = orig_wait_for  # referenced so linters know we preserve the original
