from __future__ import annotations

import asyncio
import base64
import json

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
    encode_http_stream_body,
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
async def test_nats_runner_transport_streams_response_frames() -> None:
    class _Msg:
        def __init__(self, data: bytes) -> None:
            self.data = data

    class _Subscription:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[_Msg] = asyncio.Queue()
            self.unsubscribed = False

        async def next_msg(self, timeout: float = 1.0) -> _Msg:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    class _Nats:
        def __init__(self) -> None:
            self.subscriptions: dict[str, _Subscription] = {}
            self.published: list[tuple[str, bytes]] = []
            self.request_payload: bytes | None = None
            self._inbox_counter = 0

        def new_inbox(self) -> str:
            self._inbox_counter += 1
            return f"inbox.{self._inbox_counter}"

        async def subscribe(self, subject: str) -> _Subscription:
            subscription = _Subscription()
            self.subscriptions[subject] = subscription
            return subscription

        async def request(self, subject: str, payload: bytes, timeout: float) -> _Msg:
            del subject, timeout
            self.request_payload = payload
            request = decode_http_request(payload)
            stream_reply = str(request["stream_reply"])
            await self.subscriptions[stream_reply].queue.put(
                _Msg(encode_http_stream_body(body=b"data: ready\\n\\n", more_body=True))
            )
            await self.subscriptions[stream_reply].queue.put(
                _Msg(encode_http_stream_body(body=b"", more_body=False))
            )
            return _Msg(
                encode_http_response(
                    status_code=200,
                    headers={"content-type": "text/event-stream"},
                    stream=True,
                )
            )

        async def publish(self, subject: str, payload: bytes) -> None:
            self.published.append((subject, payload))

        async def drain(self) -> None:
            return None

        async def close(self) -> None:
            return None

    fake_nats = _Nats()
    transport = NatsRunnerTransport("runner_1", nats_url="nats://fake")
    transport._nc = fake_nats  # type: ignore[attr-defined]

    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream("GET", "/v1/sessions/conv_1/stream") as response:
            body = await response.aread()

    assert response.status_code == 200
    assert body == b"data: ready\\n\\n"
    assert fake_nats.request_payload is not None
    request = decode_http_request(fake_nats.request_payload)
    assert request["path"] == "/v1/sessions/conv_1/stream"
    assert request["stream_reply"] == "inbox.1"
    assert request["stream_cancel"] == "inbox.2"
    assert fake_nats.published == [("inbox.2", b'{"type":"cancel"}')]


@pytest.mark.asyncio
async def test_nats_runner_transport_uses_read_timeout_for_stream_frames() -> None:
    class _Msg:
        def __init__(self, data: bytes) -> None:
            self.data = data

    class _Subscription:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[_Msg] = asyncio.Queue()
            self.next_timeouts: list[float] = []

        async def next_msg(self, timeout: float = 1.0) -> _Msg:
            self.next_timeouts.append(timeout)
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)

        async def unsubscribe(self) -> None:
            return None

    class _Nats:
        def __init__(self) -> None:
            self.subscriptions: dict[str, _Subscription] = {}
            self.request_timeout: float | None = None
            self._inbox_counter = 0

        def new_inbox(self) -> str:
            self._inbox_counter += 1
            return f"inbox.{self._inbox_counter}"

        async def subscribe(self, subject: str) -> _Subscription:
            subscription = _Subscription()
            self.subscriptions[subject] = subscription
            return subscription

        async def request(self, subject: str, payload: bytes, timeout: float) -> _Msg:
            del subject
            self.request_timeout = timeout
            request = decode_http_request(payload)
            stream_reply = str(request["stream_reply"])
            await self.subscriptions[stream_reply].queue.put(
                _Msg(encode_http_stream_body(body=b"data: ready\n\n", more_body=True))
            )
            await self.subscriptions[stream_reply].queue.put(
                _Msg(encode_http_stream_body(body=b"", more_body=False))
            )
            return _Msg(encode_http_response(status_code=200, stream=True))

        async def publish(self, subject: str, payload: bytes) -> None:
            del subject, payload

        async def drain(self) -> None:
            return None

        async def close(self) -> None:
            return None

    fake_nats = _Nats()
    transport = NatsRunnerTransport("runner_1", nats_url="nats://fake")
    transport._nc = fake_nats  # type: ignore[attr-defined]

    timeout = httpx.Timeout(connect=5.0, read=45.0, write=None, pool=None)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "GET",
            "/v1/sessions/conv_1/stream",
            timeout=timeout,
        ) as response:
            await response.aread()

    subscription = fake_nats.subscriptions["inbox.1"]
    assert fake_nats.request_timeout == 45.0
    assert subscription.next_timeouts == [45.0, 45.0]


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


@pytest.mark.asyncio
async def test_dispatch_nats_http_request_streams_asgi_body_frames() -> None:
    class _CancelSubscription:
        def __init__(self) -> None:
            self.unsubscribed = False

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    cancel_subscription = _CancelSubscription()
    cancel_callbacks: dict[str, object] = {}
    published: list[tuple[str, bytes]] = []

    async def cancel_subscriber(subject: str, callback: object) -> _CancelSubscription:
        cancel_callbacks[subject] = callback
        return cancel_subscription

    async def publisher(subject: str, payload: bytes) -> None:
        published.append((subject, payload))

    async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        assert scope["path"] == "/v1/sessions/conv_1/stream"
        assert scope["query_string"] == b"cursor=0"
        assert dict(scope["headers"])[b"authorization"] == b"Bearer launch-token"
        assert await receive() == {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: ready\\n\\n",
                "more_body": True,
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    request = httpx.Request(
        "GET",
        "http://runner/v1/sessions/conv_1/stream?cursor=0",
        headers={"authorization": "Bearer launch-token"},
    )
    encoded_request = await _encode_http_request(
        request,
        stream_reply="reply.1",
        stream_cancel="cancel.1",
    )

    encoded_response = await dispatch_nats_http_request(
        app,
        encoded_request,
        stream_publisher=publisher,
        cancel_subscriber=cancel_subscriber,
    )
    response = _decode_http_response(encoded_response, request=request)

    for _ in range(10):
        if len(published) >= 2:
            break
        await asyncio.sleep(0)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert cancel_callbacks.keys() == {"cancel.1"}
    assert cancel_subscription.unsubscribed is True
    assert [subject for subject, _payload in published] == ["reply.1", "reply.1"]
    first_frame = json.loads(published[0][1])
    assert base64.b64decode(first_frame["body_b64"]) == b"data: ready\\n\\n"
    assert first_frame["more_body"] is True
    second_frame = json.loads(published[1][1])
    assert base64.b64decode(second_frame["body_b64"]) == b""
    assert second_frame["more_body"] is False
