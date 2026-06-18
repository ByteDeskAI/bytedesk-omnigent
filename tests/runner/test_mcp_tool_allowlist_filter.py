"""Tests for omnigent.runner.mcp_manager.filter_schemas_by_allowlist.

Covers the per-server ``tool_allowlist`` filter that lets an agent expose a
curated subset of a high-volume MCP server's tools (BDP-2205). Tool names are
namespaced ``<server>__<tool>`` on the proxy / runner-pool path.
"""

from __future__ import annotations

from typing import Any

from omnigent.runner.mcp_manager import filter_schemas_by_allowlist
from omnigent.spec.types import AgentSpec, MCPServerConfig


def _schema(name: str) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": "", "parameters": {}}


def _spec(*servers: MCPServerConfig) -> AgentSpec:
    return AgentSpec(spec_version=1, name="test-agent", mcp_servers=list(servers))


def test_no_allowlist_returns_schemas_unchanged() -> None:
    """No server declares a tool_allowlist → schemas pass through untouched."""
    spec = _spec(MCPServerConfig(name="bytedesk-platform", transport="http", url="http://x"))
    schemas = [_schema("bytedesk-platform__a"), _schema("bytedesk-platform__b")]
    out = filter_schemas_by_allowlist(schemas, spec)
    assert out == schemas


def test_single_server_namespaced_only_allowlisted_survive() -> None:
    """One server allowlisting {a,b} keeps only its a,b namespaced tools."""
    spec = _spec(
        MCPServerConfig(
            name="bytedesk-platform",
            transport="http",
            url="http://x",
            tool_allowlist=["a", "b"],
        )
    )
    schemas = [
        _schema("bytedesk-platform__a"),
        _schema("bytedesk-platform__b"),
        _schema("bytedesk-platform__c"),
        _schema("bytedesk-platform__d"),
    ]
    out = [s["name"] for s in filter_schemas_by_allowlist(schemas, spec)]
    assert out == ["bytedesk-platform__a", "bytedesk-platform__b"]


def test_single_server_bare_names_attributed_to_only_server() -> None:
    """Bare (un-namespaced) names attribute to the sole MCP server and filter."""
    spec = _spec(
        MCPServerConfig(
            name="bytedesk-platform",
            transport="http",
            url="http://x",
            tool_allowlist=["a"],
        )
    )
    schemas = [_schema("a"), _schema("b")]
    out = [s["name"] for s in filter_schemas_by_allowlist(schemas, spec)]
    assert out == ["a"]


def test_multi_server_only_allowlisted_server_filtered() -> None:
    """With two servers, only the allowlisted server's tools are filtered."""
    spec = _spec(
        MCPServerConfig(
            name="bytedesk-platform",
            transport="http",
            url="http://x",
            tool_allowlist=["a"],
        ),
        MCPServerConfig(name="github", transport="http", url="http://y"),
    )
    schemas = [
        _schema("bytedesk-platform__a"),
        _schema("bytedesk-platform__b"),  # dropped — not on allowlist
        _schema("github__search"),  # kept — github has no allowlist
        _schema("github__pr"),  # kept
    ]
    out = [s["name"] for s in filter_schemas_by_allowlist(schemas, spec)]
    assert out == ["bytedesk-platform__a", "github__search", "github__pr"]


def test_unattributable_tools_pass_through() -> None:
    """Tools owned by no declared server fail open (kept) when another is allowlisted."""
    spec = _spec(
        MCPServerConfig(
            name="bytedesk-platform",
            transport="http",
            url="http://x",
            tool_allowlist=["a"],
        ),
        MCPServerConfig(name="github", transport="http", url="http://y"),
    )
    schemas = [
        _schema("bytedesk-platform__a"),
        _schema("unknown__tool"),  # no matching declared server → pass through
        _schema("plain_builtin"),  # bare + multiple servers → unattributable → pass through
    ]
    out = [s["name"] for s in filter_schemas_by_allowlist(schemas, spec)]
    assert out == ["bytedesk-platform__a", "unknown__tool", "plain_builtin"]
