"""Connector-managed Atlassian MCP server."""

from __future__ import annotations

import argparse
import contextlib
import json
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import FastMCP

from bytedesk_omnigent.tools.confluence_tools import BytedeskConfluenceTool
from bytedesk_omnigent.tools.jira_tools import BytedeskJiraTool
from omnigent.tools.base import ToolContext

mcp = FastMCP("atlassian")
_connection_id: str | None = None
_connection_id_context: ContextVar[str | None] = ContextVar(
    "atlassian_connector_connection_id",
    default=None,
)


def _connection() -> str:
    connection_id = _connection_id_context.get() or _connection_id
    if not connection_id:
        raise RuntimeError("missing connector connection id")
    return connection_id


@contextlib.contextmanager
def connection_context(connection_id: str):
    token = _connection_id_context.set(connection_id)
    try:
        yield
    finally:
        _connection_id_context.reset(token)


def _ctx() -> ToolContext:
    return ToolContext(task_id="connector-mcp", agent_id="connector-mcp")


def _jira(op: str, **args: Any) -> dict[str, Any]:
    tool = BytedeskJiraTool.from_config({"connection_id": _connection()})
    payload = {"op": op, **{k: v for k, v in args.items() if v is not None}}
    return json.loads(tool.invoke(json.dumps(payload), _ctx()))


def _confluence(op: str, **args: Any) -> dict[str, Any]:
    tool = BytedeskConfluenceTool.from_config({"connection_id": _connection()})
    payload = {"op": op, **{k: v for k, v in args.items() if v is not None}}
    return json.loads(tool.invoke(json.dumps(payload), _ctx()))


@mcp.tool()
def jira_search(jql: str, max_results: int = 20) -> dict[str, Any]:
    return _jira("search", jql=jql, max_results=max_results)


@mcp.tool()
def jira_get_issue(key: str) -> dict[str, Any]:
    return _jira("get_issue", key=key)


@mcp.tool()
def jira_add_comment(key: str, body: str) -> dict[str, Any]:
    return _jira("add_comment", key=key, body=body)


@mcp.tool()
def jira_transition(key: str, transition_name_or_id: str) -> dict[str, Any]:
    return _jira("transition", key=key, transition_name_or_id=transition_name_or_id)


@mcp.tool()
def jira_create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    parent: str | None = None,
) -> dict[str, Any]:
    return _jira(
        "create_issue",
        project_key=project_key,
        summary=summary,
        description=description,
        issue_type=issue_type,
        parent=parent,
    )


@mcp.tool()
def confluence_search(cql: str, limit: int = 20) -> dict[str, Any]:
    return _confluence("search", cql=cql, limit=limit)


@mcp.tool()
def confluence_get_page(page_id: str) -> dict[str, Any]:
    return _confluence("get_page", page_id=page_id)


@mcp.tool()
def confluence_create_page(
    title: str,
    body: str = "",
    space_id: str | None = None,
    space_key: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    return _confluence(
        "create_page",
        space_id=space_id,
        space_key=space_key,
        title=title,
        body=body,
        parent_id=parent_id,
    )


@mcp.tool()
def confluence_update_page(
    page_id: str,
    title: str,
    body: str = "",
    version: int | None = None,
) -> dict[str, Any]:
    return _confluence(
        "update_page",
        page_id=page_id,
        title=title,
        body=body,
        version=version,
    )


@mcp.tool()
def confluence_add_comment(page_id: str, body: str) -> dict[str, Any]:
    return _confluence("add_comment", page_id=page_id, body=body)


def main() -> None:
    global _connection_id
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    args = parser.parse_args()
    _connection_id = args.connection_id
    mcp.run("stdio")


if __name__ == "__main__":
    main()
