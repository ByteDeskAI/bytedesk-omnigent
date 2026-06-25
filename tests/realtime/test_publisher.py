"""BDP-2301 — realtime publisher lazy client + best-effort publish."""

from __future__ import annotations

import json

import pytest

from bytedesk_omnigent.realtime import publisher


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.raise_on_publish = False

    def publish(self, channel: str, message: str) -> None:
        if self.raise_on_publish:
            raise ConnectionError("redis down")
        self.published.append((channel, message))


def test_publish_serializes_payload_and_swallows_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(publisher, "_client", fake)

    publisher.publish("office:agents:t", {"type": "roster.changed", "agentId": "ag_1"})
    assert fake.published == [
        ("office:agents:t", json.dumps({"type": "roster.changed", "agentId": "ag_1"})),
    ]

    fake.raise_on_publish = True
    with caplog.at_level("WARNING", logger="bytedesk_omnigent.realtime.publisher"):
        publisher.publish("office:agents:t", {"type": "presence.changed"})
    assert any("publish to office:agents:t failed" in r.message for r in caplog.records)


def test_get_client_lazy_inits_from_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher.reset_client_for_test()
    monkeypatch.setenv("BYTEDESK_REALTIME_REDIS_URL", "redis://test:6379/0")

    created: list[str] = []

    class _RedisModule:
        class Redis:
            @staticmethod
            def from_url(url: str, **kwargs: object) -> _FakeRedis:
                created.append(url)
                return _FakeRedis()

    monkeypatch.setattr(publisher, "redis", _RedisModule, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "redis",
        _RedisModule,
    )

    client = publisher._get_client()
    assert isinstance(client, _FakeRedis)
    assert created == ["redis://test:6379/0"]
    assert publisher._get_client() is client

    publisher.reset_client_for_test()
