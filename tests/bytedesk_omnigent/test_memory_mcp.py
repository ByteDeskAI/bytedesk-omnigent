"""The slimmed memory MCP front is ADVERTISEMENT-ONLY (BDP-2458).

Execution moved server-side to the ``tools/call`` choke point
(``_handle_mcp_tools_call`` → ``memory_tool_intercept``); this stdio front exists
ONLY to declare the six ``memory__*`` tool schemas for the runner to list. These
tests pin that contract: the six tools are advertised, the bodies are inert stubs
(never the execution path), and the old httpx proxy to the deleted ``/v1/memory``
route is gone.
"""

from __future__ import annotations

import asyncio

import bytedesk_omnigent.memory_mcp as mm

_EXPECTED = {"search", "append", "put", "get", "list", "unset"}


def test_front_advertises_the_six_memory_tools() -> None:
    tools = asyncio.run(mm.mcp.list_tools())
    assert {t.name for t in tools} == _EXPECTED


def test_tool_bodies_are_inert_server_side_stubs() -> None:
    # The server intercepts memory__* by name before the runner invokes this
    # front, so the bodies are never the execution path. If one is ever reached
    # it returns a clear tripwire sentinel rather than hitting a (deleted) route.
    for result in (
        mm.search(query="x"),
        mm.append(content="x"),
        mm.put(address="org:x", content="y"),
        mm.get(address="org:x"),
        mm.list_slots(prefix="org"),
        mm.unset(address="org:x"),
    ):
        assert "error" in result and "server-side" in result["error"]


def test_front_has_no_http_proxy_dependency() -> None:
    # The slimming dropped the httpx proxy + the /v1/memory route; guard against a
    # regression that reintroduces a network call from the advertisement front.
    assert not hasattr(mm, "_post")
    assert not hasattr(mm, "_base_url")
