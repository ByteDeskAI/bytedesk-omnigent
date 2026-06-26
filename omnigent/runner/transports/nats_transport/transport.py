"""HTTPX transport that sends runner requests over NATS request/reply."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

NATS_RUNNER_REQUEST_PREFIX = "omnigent.runtime.runner"

NatsRequester = Callable[[str, bytes, float], Awaitable[bytes]]


class NatsRunnerTransport(httpx.AsyncBaseTransport):
    """Route one server-to-runner HTTP request over NATS request/reply.

    The transport preserves the existing ``httpx.AsyncClient`` contract used by
    session/resource routes, but the wire substrate is NATS instead of the
    removed runner WebSocket tunnel.
    """

    def __init__(
        self,
        runner_id: str,
        *,
        nats_url: str | None = None,
        timeout_s: float = 300.0,
        auth_token: str | None = None,
        requester: NatsRequester | None = None,
    ) -> None:
        self._runner_id = runner_id
        self._nats_url = nats_url or os.environ.get("OMNIGENT_NATS_URL", "")
        self._timeout_s = timeout_s
        self._auth_token = auth_token
        self._requester = requester
        self._nc: Any = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        subject = f"{NATS_RUNNER_REQUEST_PREFIX}.{self._runner_id}.http"
        payload = await _encode_http_request(request, auth_token=self._auth_token)
        timeout_s = _request_timeout_s(request, self._timeout_s)
        try:
            raw_response = await self._request(subject, payload, timeout_s)
        except Exception as exc:
            raise httpx.ConnectError(
                f"runner {self._runner_id!r} unavailable over NATS: {exc}",
                request=request,
            ) from exc
        return _decode_http_response(raw_response, request=request)

    async def aclose(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            await self._nc.close()
            self._nc = None

    async def _request(self, subject: str, payload: bytes, timeout_s: float) -> bytes:
        if self._requester is not None:
            return await self._requester(subject, payload, timeout_s)
        if not self._nats_url.strip():
            raise RuntimeError("OMNIGENT_NATS_URL is required for runner transport")
        if self._nc is None:
            try:
                import nats
            except ImportError as exc:
                raise RuntimeError(
                    "nats-py is required for runner transport; install omnigent[coordination]"
                ) from exc
            self._nc = await nats.connect(
                servers=[self._nats_url],
                name=f"omnigent-runner-transport-{self._runner_id}",
                max_reconnect_attempts=-1,
            )
        msg = await self._nc.request(subject, payload, timeout=timeout_s)
        return msg.data


async def _encode_http_request(
    request: httpx.Request,
    *,
    auth_token: str | None = None,
) -> bytes:
    body = await request.aread()
    headers = [
        [key.decode("latin-1"), value.decode("latin-1")]
        for key, value in request.headers.raw
    ]
    if auth_token and not any(key.lower() == "authorization" for key, _value in headers):
        headers.append(["authorization", f"Bearer {auth_token}"])
    payload = {
        "method": request.method,
        "path": request.url.raw_path.decode("ascii", errors="ignore"),
        "headers": headers,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _decode_http_response(payload: bytes, *, request: httpx.Request) -> httpx.Response:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("runner NATS response must be a JSON object")
    body = base64.b64decode(str(data.get("body_b64") or ""))
    headers = {
        str(key): str(value)
        for key, value in dict(data.get("headers") or {}).items()
    }
    return httpx.Response(
        int(data.get("status_code") or 500),
        headers=headers,
        content=body,
        request=request,
    )


def _request_timeout_s(request: httpx.Request, default: float) -> float:
    timeout = request.extensions.get("timeout")
    if not isinstance(timeout, dict):
        return default
    candidates: list[float] = []
    for key in ("read", "connect"):
        value = timeout.get(key)
        if isinstance(value, int | float) and value > 0:
            candidates.append(float(value))
    return min(candidates) if candidates else default


def encode_http_response(
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> bytes:
    """Encode a runner-side response for tests and the NATS subscriber."""
    return json.dumps(
        {
            "status_code": status_code,
            "headers": headers or {},
            "body_b64": base64.b64encode(body).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def decode_http_request(payload: bytes) -> dict[str, Any]:
    """Decode a server-side NATS runner request."""
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("runner NATS request must be a JSON object")
    data["body"] = base64.b64decode(str(data.get("body_b64") or ""))
    return data
