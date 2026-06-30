"""Server-side connector MCP tool interception."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from bytedesk_omnigent.connectors.manifests import ConnectorManifest
from bytedesk_omnigent.connectors.registry import build_connector_registry
from bytedesk_omnigent.connectors.store import (
    ConnectorAgentGrant,
    ConnectorConnection,
    ConnectorServiceState,
    get_connector_store,
)

_logger = logging.getLogger(__name__)

_PREFIX_TO_PROVIDER = {
    "atlassian__": "atlassian",
    "google__": "google_workspace",
}


@dataclass(frozen=True)
class _ResolvedConnectorTool:
    connection: ConnectorConnection
    grant: ConnectorAgentGrant
    service: ConnectorServiceState
    bare_tool: str


def connector_tool_prefixes() -> tuple[str, ...]:
    """Prefixes claimed by first-party connector MCP fronts."""

    return tuple(_PREFIX_TO_PROVIDER)


def execute_connector_tool(
    namespaced_name: str,
    arguments: dict[str, Any] | None,
    *,
    caller_agent_id: str | None,
    caller_department: str | None = None,
) -> str | None:
    """Execute a connector MCP call through the Omnigent connector store.

    Connector stdio MCP servers are schema advertisement fronts. Runtime calls
    execute here so the server can resolve the verified caller's agent grants and
    provider credentials from the extension-owned connector store.
    """

    del caller_department
    prefix, provider, bare_tool = _split_connector_tool(namespaced_name)
    if prefix is None or provider is None or bare_tool is None:
        return None
    if not caller_agent_id:
        return _error("connector_agent_identity_required", namespaced_name=namespaced_name)

    try:
        resolved = _resolve_connector_tool(
            provider=provider,
            bare_tool=bare_tool,
            caller_agent_id=caller_agent_id,
        )
        if isinstance(resolved, str):
            return _error(resolved, namespaced_name=namespaced_name)
        result = _invoke_connector_tool(
            provider=provider,
            connection_id=resolved.connection.id,
            bare_tool=bare_tool,
            arguments=arguments or {},
        )
        return json.dumps(result)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("connector tool %r failed", namespaced_name, exc_info=True)
        return _error(
            "connector_tool_failed",
            namespaced_name=namespaced_name,
            message=str(exc),
        )


def _split_connector_tool(
    namespaced_name: str,
) -> tuple[str | None, str | None, str | None]:
    for prefix, provider in _PREFIX_TO_PROVIDER.items():
        if namespaced_name.startswith(prefix):
            bare = namespaced_name[len(prefix) :]
            return prefix, provider, bare
    return None, None, None


def _manifest_tool_index(
    manifest: ConnectorManifest,
) -> dict[str, tuple[str, str]]:
    return {
        tool.mcp_tool: (service.key, tool.key)
        for service in manifest.services
        for tool in service.tools
    }


def _resolve_connector_tool(
    *,
    provider: str,
    bare_tool: str,
    caller_agent_id: str,
) -> _ResolvedConnectorTool | str:
    registry = build_connector_registry()
    manifest = registry.get(provider)
    if manifest is None:
        return "connector_provider_not_found"
    manifest_tools = _manifest_tool_index(manifest)
    target = manifest_tools.get(bare_tool)
    if target is None:
        return "connector_tool_not_found"
    service_key, tool_key = target

    store = get_connector_store()
    matches: list[_ResolvedConnectorTool] = []
    for grant in store.list_agent_grants(agent_id=caller_agent_id):
        if not grant.enabled or grant.status != "active":
            continue
        if grant.service_key != service_key or grant.tool_key != tool_key:
            continue
        connection = store.get_connection(grant.connection_id)
        if connection is None or connection.provider != provider:
            continue
        service = next(
            (
                svc
                for svc in store.list_services(connection.id)
                if svc.service_key == service_key
            ),
            None,
        )
        if service is None or not service.enabled or service.status != "ready":
            continue
        matches.append(
            _ResolvedConnectorTool(
                connection=connection,
                grant=grant,
                service=service,
                bare_tool=bare_tool,
            )
        )

    connection_ids = {match.connection.id for match in matches}
    if not connection_ids:
        return "connector_tool_not_granted"
    if len(connection_ids) > 1:
        return "connector_tool_connection_ambiguous"
    return matches[0]


def _invoke_connector_tool(
    *,
    provider: str,
    connection_id: str,
    bare_tool: str,
    arguments: dict[str, Any],
) -> Any:
    if provider == "atlassian":
        from bytedesk_omnigent.connectors import atlassian_mcp

        func = getattr(atlassian_mcp, bare_tool, None)
        if not callable(func):
            return {"ok": False, "error": "connector_tool_not_implemented"}
        with atlassian_mcp.connection_context(connection_id):
            return func(**arguments)
    if provider == "google_workspace":
        from bytedesk_omnigent.connectors import google_workspace_mcp

        func = getattr(google_workspace_mcp, bare_tool, None)
        if not callable(func):
            return {"ok": False, "error": "connector_tool_not_implemented"}
        with google_workspace_mcp.connection_context(connection_id):
            return func(**arguments)
    return {"ok": False, "error": "connector_provider_not_implemented"}


def _error(error: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": error, **extra})


__all__ = [
    "connector_tool_prefixes",
    "execute_connector_tool",
]
