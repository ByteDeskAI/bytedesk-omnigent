"""Unit tests for the stdio MCP front (BDP-2459 unified memory).

Each tool POSTs to the right ``/v1/memory/<path>`` URL with the right body and
returns the route's JSON. httpx is stubbed with a MockTransport so no server is
needed; we patch ``httpx.Client`` to inject the transport + capture the request.
"""

from __future__ import annotations

import json

import httpx
import pytest

import bytedesk_omnigent.memory_mcp as mm


@pytest.fixture()
def captured(monkeypatch):
    """Patch httpx.Client so every tool call is captured + answered by a stub."""
    box: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        box["url"] = str(request.url)
        box["method"] = request.method
        box["body"] = json.loads(request.content.decode() or "{}")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real = httpx.Client
    monkeypatch.setattr(
        mm.httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": transport})
    )
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://srv")
    return box


def test_search_posts_recall_spanning_both(captured) -> None:
    mm.search(query="pricing")
    assert captured["url"] == "http://srv/v1/memory/recall"
    assert captured["body"] == {
        "query": "pricing",
        "scope": "team",
        "name": "org-context",
        "kind": "all",  # search spans ambient + addressable by default
        "limit": 10,
    }


def test_search_kind_override(captured) -> None:
    mm.search(query="x", kind="addressable", scope="topic", name="dept:eng", limit=3)
    assert captured["body"] == {
        "query": "x",
        "scope": "topic",
        "name": "dept:eng",
        "kind": "addressable",
        "limit": 3,
    }


def test_append_posts_append(captured) -> None:
    mm.append(content="we ship fridays", scope="topic", name="dept:eng", weight=2.0)
    assert captured["url"] == "http://srv/v1/memory/append"
    assert captured["body"] == {
        "content": "we ship fridays",
        "scope": "topic",
        "name": "dept:eng",
        "weight": 2.0,
    }


def test_put_posts_put(captured) -> None:
    mm.put(address="org:charter", content="be the best", confidence=0.9)
    assert captured["url"] == "http://srv/v1/memory/put"
    assert captured["body"] == {
        "address": "org:charter",
        "content": "be the best",
        "weight": 1.0,
        "confidence": 0.9,
        "source_conversation_id": None,
    }


def test_get_posts_get(captured) -> None:
    mm.get(address="org:charter")
    assert captured["url"] == "http://srv/v1/memory/get"
    assert captured["body"] == {"address": "org:charter"}


def test_list_posts_list(captured) -> None:
    mm.list_slots(prefix="dept:eng")
    assert captured["url"] == "http://srv/v1/memory/list"
    assert captured["body"] == {"prefix": "dept:eng"}


def test_unset_posts_unset(captured) -> None:
    mm.unset(address="org:charter")
    assert captured["url"] == "http://srv/v1/memory/unset"
    assert captured["body"] == {"address": "org:charter"}


def test_non_json_response_is_wrapped(monkeypatch) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    real = httpx.Client
    monkeypatch.setattr(
        mm.httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": transport})
    )
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://srv")
    out = mm.get(address="org:x")
    assert "error" in out and "non-JSON" in out["error"]


def test_base_url_precedence(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    assert mm._base_url() == "http://omnigent-server.bytedesk.svc.cluster.local"
    monkeypatch.setenv("OMNIGENT_SERVER_URL", "http://host-side")
    assert mm._base_url() == "http://host-side"
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://self/")
    assert mm._base_url() == "http://self"


@pytest.mark.asyncio
async def test_tools_list_exposes_six() -> None:
    tools = await mm.mcp.list_tools()
    assert sorted(t.name for t in tools) == ["append", "get", "list", "put", "search", "unset"]
