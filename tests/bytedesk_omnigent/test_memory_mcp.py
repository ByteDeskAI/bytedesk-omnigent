"""Unit tests for the stdio MCP front (BDP-2457 F1).

Each tool must POST to the right ``/v1/memory/<path>`` URL with the right body
and return the route's JSON verbatim. httpx is stubbed with a MockTransport so no
server is needed; we patch ``httpx.Client`` to inject the transport + capture the
request.
"""

from __future__ import annotations

import json

import httpx
import pytest

import bytedesk_omnigent.memory_mcp as mm


@pytest.fixture()
def captured(monkeypatch):
    """Patch httpx.Client so every tool call is captured + answered by a stub.

    Returns a dict that, after a tool call, holds the request ``url`` + ``body``.
    """
    box: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        box["url"] = str(request.url)
        box["method"] = request.method
        box["body"] = json.loads(request.content.decode() or "{}")
        return httpx.Response(200, json={"ok": True, "echo": box["body"]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mm.httpx, "Client", client_factory)
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://srv")
    return box


def test_recall_posts_to_recall_route(captured) -> None:
    out = mm.recall(query="release cadence", scope="team", name="org-context", limit=5)
    assert captured["method"] == "POST"
    assert captured["url"] == "http://srv/v1/memory/recall"
    assert captured["body"] == {
        "query": "release cadence",
        "scope": "team",
        "name": "org-context",
        "limit": 5,
    }
    assert out["ok"] is True


def test_recall_defaults_to_org_blackboard(captured) -> None:
    mm.recall(query="x")
    assert captured["body"]["scope"] == "team"
    assert captured["body"]["name"] == "org-context"
    assert captured["body"]["limit"] == 10


def test_append_posts_to_append_route(captured) -> None:
    mm.append(content="we ship fridays", scope="topic", name="dept:engineering", weight=2.0)
    assert captured["url"] == "http://srv/v1/memory/append"
    assert captured["body"] == {
        "content": "we ship fridays",
        "scope": "topic",
        "name": "dept:engineering",
        "weight": 2.0,
    }


def test_append_defaults(captured) -> None:
    mm.append(content="c")
    body = captured["body"]
    assert body["scope"] == "team"
    assert body["name"] == "org-context"
    assert body["weight"] == 1.0


def test_compartments_posts_empty_body(captured) -> None:
    mm.compartments()
    assert captured["url"] == "http://srv/v1/memory/compartments"
    assert captured["body"] == {}


def test_memory_get_posts_address(captured) -> None:
    mm.memory_get(address="org:charter")
    assert captured["url"] == "http://srv/v1/memory/get"
    assert captured["body"] == {"address": "org:charter"}


def test_memory_put_posts_address_and_content(captured) -> None:
    mm.memory_put(
        address="org:charter", content="be the best", confidence=0.9, source_conversation_id="c1"
    )
    assert captured["url"] == "http://srv/v1/memory/put"
    assert captured["body"] == {
        "address": "org:charter",
        "content": "be the best",
        "weight": 1.0,
        "confidence": 0.9,
        "source_conversation_id": "c1",
    }


def test_memory_unset_posts_address(captured) -> None:
    mm.memory_unset(address="dept:engineering:oncall")
    assert captured["url"] == "http://srv/v1/memory/unset"
    assert captured["body"] == {"address": "dept:engineering:oncall"}


def test_non_json_response_is_wrapped(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        mm.httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": transport})
    )
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://srv")

    out = mm.recall(query="x")
    assert "error" in out
    assert "non-JSON" in out["error"]


def test_base_url_precedence(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    assert mm._base_url() == "http://omnigent-server.bytedesk.svc.cluster.local"
    monkeypatch.setenv("OMNIGENT_SERVER_URL", "http://host-side")
    assert mm._base_url() == "http://host-side"
    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://self/")
    assert mm._base_url() == "http://self"  # SELF wins + trailing slash stripped


@pytest.mark.asyncio
async def test_tools_list_exposes_all_tools() -> None:
    tools = await mm.mcp.list_tools()
    assert sorted(t.name for t in tools) == [
        "append",
        "compartments",
        "memory_get",
        "memory_put",
        "memory_unset",
        "recall",
    ]
