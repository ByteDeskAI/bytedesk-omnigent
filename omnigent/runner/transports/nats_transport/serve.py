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
    encode_http_stream_body,
    encode_http_stream_error,
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
NatsPublisher = Callable[[str, bytes], Awaitable[None]]
_STREAM_TASKS: set[asyncio.Task[None]] = set()


async def dispatch_nats_http_request(
    app: _ASGIApp,
    payload: bytes,
    *,
    stream_publisher: NatsPublisher | None = None,
    cancel_subscriber: Callable[[str, Callable[[], None]], Awaitable[Any]] | None = None,
) -> bytes:
    """Dispatch one NATS-carried HTTP request through the runner ASGI app."""
    request = decode_http_request(payload)
    stream_reply = request.get("stream_reply")
    if isinstance(stream_reply, str) and stream_publisher is not None:
        return await _dispatch_nats_streaming_http_request(
            app,
            request,
            stream_reply=stream_reply,
            stream_publisher=stream_publisher,
            cancel_subscriber=cancel_subscriber,
        )
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

    async def _cancel_subscriber(cancel_subject: str, on_cancel: Callable[[], None]) -> Any:
        async def _handle_cancel(_msg: Any) -> None:
            on_cancel()

        return await nc.subscribe(cancel_subject, cb=_handle_cancel)

    async def _handle_msg(msg: Any) -> None:
        if on_activity is not None:
            on_activity()
        try:
            response = await dispatch_nats_http_request(
                app,
                msg.data,
                stream_publisher=nc.publish,
                cancel_subscriber=_cancel_subscriber,
            )
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


async def _dispatch_nats_streaming_http_request(
    app: _ASGIApp,
    request: dict[str, Any],
    *,
    stream_reply: str,
    stream_publisher: NatsPublisher,
    cancel_subscriber: Callable[[str, Callable[[], None]], Awaitable[Any]] | None,
) -> bytes:
    """Dispatch one streaming ASGI response over NATS body frames."""
    status_code = 500
    response_headers: dict[str, str] = {}
    request_body_sent = False
    disconnected = asyncio.Event()
    response_started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    cancel_subscription: Any | None = None

    cancel_subject = request.get("stream_cancel")
    if isinstance(cancel_subject, str) and cancel_subscriber is not None:
        cancel_subscription = await cancel_subscriber(cancel_subject, disconnected.set)

    async def _receive() -> dict[str, Any]:
        nonlocal request_body_sent
        if not request_body_sent:
            request_body_sent = True
            return {
                "type": "http.request",
                "body": request.get("body") or b"",
                "more_body": False,
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, Any]) -> None:
        nonlocal response_headers, status_code
        msg_type = message.get("type")
        if msg_type == "http.response.start":
            status_code = int(message.get("status") or 500)
            response_headers = _encode_asgi_headers(message.get("headers"))
            if not response_started.done():
                response_started.set_result(None)
            return
        if msg_type != "http.response.body":
            return
        if not response_started.done():
            response_started.set_result(None)
        await stream_publisher(
            stream_reply,
            encode_http_stream_body(
                body=bytes(message.get("body") or b""),
                more_body=bool(message.get("more_body")),
            ),
        )

    async def _run_app() -> None:
        try:
            await app(_asgi_scope_from_request(request), _receive, _send)
        except Exception as exc:
            _logger.exception("runner NATS stream request failed")
            if not response_started.done():
                response_started.set_result(None)
            await stream_publisher(stream_reply, encode_http_stream_error(str(exc)))
        finally:
            disconnected.set()
            if cancel_subscription is not None:
                with contextlib.suppress(Exception):
                    await cancel_subscription.unsubscribe()

    task = asyncio.create_task(_run_app(), name="runner-nats-stream")
    _STREAM_TASKS.add(task)
    task.add_done_callback(_STREAM_TASKS.discard)
    await asyncio.wait_for(response_started, timeout=5.0)
    return encode_http_response(
        status_code=status_code,
        headers=response_headers,
        stream=True,
    )


def _asgi_scope_from_request(request: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(request.get("path") or "/")
    path, _, query_string = raw_path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": str(request.get("method") or "GET").upper(),
        "scheme": "http",
        "path": path or "/",
        "raw_path": (path or "/").encode("ascii", errors="ignore"),
        "query_string": query_string.encode("ascii", errors="ignore"),
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _decode_headers(request.get("headers"))
        ],
        "client": ("nats", 0),
        "server": ("runner", 80),
    }


def _encode_asgi_headers(raw_headers: object) -> dict[str, str]:
    if not isinstance(raw_headers, list):
        return {}
    headers: dict[str, str] = {}
    for item in raw_headers:
        if (
            isinstance(item, tuple | list)
            and len(item) == 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            headers[item[0].decode("latin-1")] = item[1].decode("latin-1")
    return headers


__all__ = [
    "RUNNER_NATS_REJECTION_PREFIX",
    "dispatch_nats_http_request",
    "serve_runner_nats",
]
