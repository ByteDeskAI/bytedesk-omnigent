from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner.transports.nats_transport.serve import dispatch_nats_http_request
from omnigent.runner.transports.nats_transport.transport import (
    NATS_RUNNER_REQUEST_PREFIX,
    NatsRunnerTransport,
    _decode_http_response,
    _encode_http_request,
    decode_http_request,
    encode_http_response,
)


@pytest.mark.asyncio
async def test_nats_runner_transport_round_trips_http_request() -> None:
    seen: dict[str, object] = {}

    async def requester(subject: str, payload: bytes, timeout_s: float) -> bytes:
        seen["subject"] = subject
        seen["timeout_s"] = timeout_s
        seen["request"] = decode_http_request(payload)
        return encode_http_response(
            status_code=201,
            headers={"content-type": "application/json"},
            body=b'{"ok":true}',
        )

    client = httpx.AsyncClient(
        transport=NatsRunnerTransport(
            "runner_1",
            requester=requester,
            timeout_s=12.0,
        ),
        base_url="http://runner",
    )
    try:
        response = await client.post("/v1/work?x=1", json={"hello": "world"})
    finally:
        await client.aclose()

    assert seen["subject"] == f"{NATS_RUNNER_REQUEST_PREFIX}.runner_1.http"
    assert seen["timeout_s"] == 5.0
    assert seen["request"]["method"] == "POST"
    assert seen["request"]["path"] == "/v1/work?x=1"
    assert seen["request"]["body"] == b'{"hello":"world"}'
    assert response.status_code == 201
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_nats_runner_transport_uses_request_timeout() -> None:
    seen: dict[str, float] = {}

    async def requester(subject: str, payload: bytes, timeout_s: float) -> bytes:
        del subject, payload
        seen["timeout_s"] = timeout_s
        return encode_http_response(status_code=200, body=b'{"ok":true}')

    async with httpx.AsyncClient(
        transport=NatsRunnerTransport(
            "runner_1",
            requester=requester,
            timeout_s=12.0,
        ),
        base_url="http://runner",
    ) as client:
        response = await client.get("/health", timeout=0.25)

    assert response.status_code == 200
    assert seen["timeout_s"] == 0.25


@pytest.mark.asyncio
async def test_nats_runner_transport_adds_launch_auth_token() -> None:
    seen: dict[str, object] = {}

    async def requester(subject: str, payload: bytes, timeout_s: float) -> bytes:
        del subject, timeout_s
        seen["request"] = decode_http_request(payload)
        return encode_http_response(status_code=204)

    async with httpx.AsyncClient(
        transport=NatsRunnerTransport(
            "runner_1",
            auth_token="launch-token",
            requester=requester,
        ),
        base_url="http://runner",
    ) as client:
        response = await client.get("/v1/work")

    assert response.status_code == 204
    headers = dict(seen["request"]["headers"])  # type: ignore[index]
    assert headers["authorization"] == "Bearer launch-token"


@pytest.mark.asyncio
async def test_dispatch_nats_http_request_runs_runner_asgi_app() -> None:
    app = FastAPI()

    @app.post("/v1/echo")
    async def echo(body: dict[str, str]) -> dict[str, object]:
        return {"body": body, "served_by": "runner"}

    request = httpx.Request(
        "POST",
        "http://runner/v1/echo",
        headers={"content-type": "application/json"},
        content=b'{"message":"hello"}',
    )
    encoded_request = await _encode_http_request(request)

    encoded_response = await dispatch_nats_http_request(app, encoded_request)
    response = _decode_http_response(encoded_response, request=request)

    assert response.status_code == 200
    assert response.json() == {
        "body": {"message": "hello"},
        "served_by": "runner",
    }
