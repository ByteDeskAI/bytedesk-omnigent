"""The org MCP front advertises org-source-of-truth tools only.

Execution is server-side so calls read the live AgentStore / Work Force stores
with the verified caller identity. The stdio front only declares schemas for
runner discovery.
"""

from __future__ import annotations

import asyncio

import bytedesk_omnigent.org_mcp as org_mcp

_EXPECTED = {"find_agent", "get_chart", "get_effective_access"}


def test_front_advertises_org_tools() -> None:
    tools = asyncio.run(org_mcp.mcp.list_tools())

    assert {t.name for t in tools} == _EXPECTED


def test_tool_bodies_are_inert_server_side_stubs() -> None:
    for result in (
        org_mcp.get_chart(),
        org_mcp.find_agent(query="maya"),
        org_mcp.get_effective_access(),
    ):
        assert "error" in result and "server-side" in result["error"]
