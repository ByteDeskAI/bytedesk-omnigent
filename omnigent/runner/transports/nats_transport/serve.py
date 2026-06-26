"""Runner-side NATS request/reply server for control-plane HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from omnigent.runner.transports.nats_transport.transport import (
    NATS_RUNNER_REQUEST_PREFIX,
    decode_http_request,
    encode_http_response,
)

_logger = logging.getLogger(__name__)

_ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

RUNNER_NATS_REJECTION_PREFIX = "runner NATS control plane rejected "


async def dispatch_nats_http_request(app: _ASGIApp, payload: bytes) -> bytes:
    """Dispatch one NATS-carried HTTP request through the runner ASGI app."""
    request = decode_http_request(payload)
    headers = _decode_headers(request.get("headers"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("nats", 0)),
        base_url="http://runner",
    ) as client:
        response = await client.request(
            str(request.get("method") or "GET"),
            str(request.get("path") or "/"),
            headers=headers,
            content=request.get("body") or b"",
        )
    return encode_http_response(
        status_code=response.status_code,
        headers=dict(response.headers),
        body=response.content,
    )


async def serve_runner_nats(
    app: _ASGIApp,
    *,
    runner_id: str,
    nats_url: str | None = None,
    on_activity: Callable[[], None] | None = None,
    on_reconnect: Callable[[], Awaitable[None] | None] | None = None,
) -> None:
    """Serve runner control-plane HTTP over NATS request/reply."""
    resolved_url = (nats_url or os.environ.get("OMNIGENT_NATS_URL", "")).strip()
    if not resolved_url:
        raise RuntimeError("OMNIGENT_NATS_URL is required for runner NATS transport")
    try:
        import nats
    except ImportError as exc:
        raise RuntimeError(
            "nats-py is required for runner NATS transport; install omnigent[coordination]"
        ) from exc

    async def _reconnected_cb() -> None:
        if on_reconnect is None:
            return
        result = on_reconnect()
        if inspect.isawaitable(result):
            await result

    connect_kwargs: dict[str, Any] = {
        "servers": [resolved_url],
        "name": f"omnigent-runner-{runner_id}",
        "max_reconnect_attempts": -1,
    }
    if on_reconnect is not None:
        connect_kwargs["reconnected_cb"] = _reconnected_cb
    nc = await nats.connect(**connect_kwargs)
    subject = f"{NATS_RUNNER_REQUEST_PREFIX}.{runner_id}.http"

    async def _handle_msg(msg: Any) -> None:
        if on_activity is not None:
            on_activity()
        try:
            response = await dispatch_nats_http_request(app, msg.data)
        except Exception:
            _logger.exception("runner NATS request failed for %s", runner_id)
            response = encode_http_response(
                status_code=500,
                headers={"content-type": "application/json"},
                body=b'{"error":"runner request failed"}',
            )
        if msg.reply:
            await nc.publish(msg.reply, response)

    subscription = await nc.subscribe(subject, cb=_handle_msg)
    _logger.info("runner NATS control plane subscribed on %s", subject)
    try:
        await asyncio.Future()
    finally:
        with contextlib.suppress(Exception):
            await subscription.unsubscribe()
        await nc.drain()
        await nc.close()


def _decode_headers(raw_headers: object) -> list[tuple[str, str]]:
    if not isinstance(raw_headers, list):
        return []
    headers: list[tuple[str, str]] = []
    for item in raw_headers:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            headers.append((item[0], item[1]))
    return headers


__all__ = [
    "RUNNER_NATS_REJECTION_PREFIX",
    "dispatch_nats_http_request",
    "serve_runner_nats",
]
