"""STDIO MCP front that advertises organization source-of-truth tools.

The real work executes server-side through :mod:`bytedesk_omnigent.org_tool_intercept`.
This front only publishes schemas so every agent can discover the tools through
the normal MCP path.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("org")

_SERVER_SIDE_STUB = {
    "error": "org tools execute server-side at the tools/call choke point; "
    "this advertisement-only stub must not be invoked"
}


@mcp.tool()
def get_chart(
    department: str | None = None,
    include_system: bool = False,
    include_harness: bool = False,
    include_workflow: bool = False,
) -> dict:
    """Return the current organization chart from the live AgentStore.

    By default this returns employee agents grouped by department. Set the
    include flags only when you need to inspect non-employee tiers.
    """
    del department, include_system, include_harness, include_workflow
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def find_agent(
    query: str,
    department: str | None = None,
    category: str = "employee",
    limit: int = 10,
) -> dict:
    """Find agents in the current roster by id, name, display name, title, or department."""
    del query, department, category, limit
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def get_effective_access(
    agent_id: str | None = None,
    include_direct_grants: bool = True,
) -> dict:
    """Return an agent's effective Work Force access and direct connector grants.

    Omit ``agent_id`` to inspect the calling agent.
    """
    del agent_id, include_direct_grants
    return dict(_SERVER_SIDE_STUB)


def main() -> None:
    """Run the stdio MCP server."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
