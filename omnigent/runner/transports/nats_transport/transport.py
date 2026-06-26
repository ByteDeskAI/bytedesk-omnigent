"""HTTPX transport that sends runner requests over NATS request/reply."""

from __future__ import annotations

import base64
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

NATS_RUNNER_REQUEST_PREFIX = "omnigent.runtime.runner"

NatsRequester = Callable[[str, bytes, float], Awaitable[bytes]]

_STREAM_FRAME_BODY = "body"
_STREAM_FRAME_ERROR = "error"


class NatsRunnerTransport(httpx.AsyncBaseTransport):
    """Route one server-to-runner HTTP request over NATS request/reply.

    The transport preserves the existing ``httpx.AsyncClient`` contract used by
    session/resource routes while using NATS as the runner control substrate.
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
        timeout_s = _request_timeout_s(request, self._timeout_s)
        if self._requester is None and _request_wants_stream(request):
            return await self._handle_stream_request(request, subject, timeout_s)
        payload = await _encode_http_request(request, auth_token=self._auth_token)
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
        nc = await self._ensure_nc()
        msg = await nc.request(subject, payload, timeout=timeout_s)
        return msg.data

    async def _ensure_nc(self) -> Any:
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
        return self._nc

    async def _handle_stream_request(
        self,
        request: httpx.Request,
        subject: str,
        timeout_s: float,
    ) -> httpx.Response:
        nc = await self._ensure_nc()
        stream_reply = nc.new_inbox()
        cancel_subject = nc.new_inbox()
        subscription = await nc.subscribe(stream_reply)
        payload = await _encode_http_request(
            request,
            auth_token=self._auth_token,
            stream_reply=stream_reply,
            stream_cancel=cancel_subject,
        )
        try:
            raw_response = (await nc.request(subject, payload, timeout=timeout_s)).data
        except Exception as exc:
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()
            # Wrap nats errors (NoRespondersError = dead runner subject,
            # TimeoutError, …) into httpx.ConnectError so the stream path fails
            # the SAME way the unary path does (handle_async_request above), and
            # the heal detector catches both read paths uniformly (BDP-2579 F2).
            raise httpx.ConnectError(
                f"runner {self._runner_id!r} unavailable over NATS: {exc}",
                request=request,
            ) from exc
        if not _encoded_http_response_is_stream(raw_response):
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()
            return _decode_http_response(raw_response, request=request)
        return _decode_http_response(
            raw_response,
            request=request,
            stream=_NatsRunnerResponseStream(
                subscription=subscription,
                nc=nc,
                cancel_subject=cancel_subject,
                timeout_s=timeout_s,
            ),
        )


async def _encode_http_request(
    request: httpx.Request,
    *,
    auth_token: str | None = None,
    stream_reply: str | None = None,
    stream_cancel: str | None = None,
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
    if stream_reply is not None:
        payload["stream_reply"] = stream_reply
    if stream_cancel is not None:
        payload["stream_cancel"] = stream_cancel
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _decode_http_response(
    payload: bytes,
    *,
    request: httpx.Request,
    stream: httpx.AsyncByteStream | None = None,
) -> httpx.Response:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("runner NATS response must be a JSON object")
    body = base64.b64decode(str(data.get("body_b64") or ""))
    headers = {
        str(key): str(value)
        for key, value in dict(data.get("headers") or {}).items()
    }
    if stream is not None:
        return httpx.Response(
            int(data.get("status_code") or 500),
            headers=headers,
            stream=stream,
            request=request,
        )
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
    read_timeout = timeout.get("read")
    if isinstance(read_timeout, int | float) and read_timeout > 0:
        return float(read_timeout)
    connect_timeout = timeout.get("connect")
    if isinstance(connect_timeout, int | float) and connect_timeout > 0:
        return float(connect_timeout)
    return default


def encode_http_response(
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    stream: bool = False,
) -> bytes:
    """Encode a runner-side response for tests and the NATS subscriber."""
    payload: dict[str, Any] = {
        "status_code": status_code,
        "headers": headers or {},
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    if stream:
        payload["stream"] = True
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _encoded_http_response_is_stream(payload: bytes) -> bool:
    data = json.loads(payload.decode("utf-8"))
    return isinstance(data, dict) and data.get("stream") is True


def encode_http_stream_body(*, body: bytes = b"", more_body: bool = False) -> bytes:
    return json.dumps(
        {
            "type": _STREAM_FRAME_BODY,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "more_body": more_body,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def encode_http_stream_error(message: str) -> bytes:
    return json.dumps(
        {
            "type": _STREAM_FRAME_ERROR,
            "message": message,
            "more_body": False,
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


def _request_wants_stream(request: httpx.Request) -> bool:
    return request.method.upper() == "GET" and request.url.path.endswith("/stream")


class _NatsRunnerResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        *,
        subscription: Any,
        nc: Any,
        cancel_subject: str,
        timeout_s: float,
    ) -> None:
        self._subscription = subscription
        self._nc = nc
        self._cancel_subject = cancel_subject
        self._timeout_s = timeout_s
        self._closed = False

    async def __aiter__(self) -> Any:
        try:
            while True:
                try:
                    msg = await self._subscription.next_msg(timeout=self._timeout_s)
                except TimeoutError as exc:
                    raise httpx.ReadTimeout(
                        "timed out waiting for runner NATS stream frame"
                    ) from exc
                frame = json.loads(msg.data.decode("utf-8"))
                if not isinstance(frame, dict):
                    continue
                frame_type = frame.get("type")
                if frame_type == _STREAM_FRAME_ERROR:
                    raise httpx.ReadError(str(frame.get("message") or "runner stream failed"))
                if frame_type != _STREAM_FRAME_BODY:
                    continue
                body = base64.b64decode(str(frame.get("body_b64") or ""))
                more_body = bool(frame.get("more_body"))
                if body:
                    yield body
                if not more_body:
                    return
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._nc.publish(
                self._cancel_subject,
                json.dumps({"type": "cancel"}, separators=(",", ":")).encode("utf-8"),
            )
        with contextlib.suppress(Exception):
            await self._subscription.unsubscribe()
