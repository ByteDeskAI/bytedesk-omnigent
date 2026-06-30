from __future__ import annotations

import json
from typing import Any

from bytedesk_omnigent.connectors.providers import GoogleWorkspaceConnectorProvider
from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore
from bytedesk_omnigent.connectors.tool_intercept import execute_connector_tool


def test_connector_tool_interceptor_executes_granted_google_tool(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    connection = store.upsert_connection(
        provider="google_workspace",
        display_name="ByteDesk Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    provider.bootstrap_services(store, connection, ["drive"])
    store.upsert_agent_grant(
        connection_id=connection.id,
        agent_id="chief-of-staff",
        service_key="drive",
        tool_key="search",
        enabled=True,
    )
    captured: dict[str, Any] = {}

    def fake_drive_search(query: str, page_size: int = 10) -> dict[str, Any]:
        from bytedesk_omnigent.connectors import google_workspace_mcp

        captured["connection_id"] = google_workspace_mcp._connection()
        captured["query"] = query
        captured["page_size"] = page_size
        return {"ok": True, "files": [{"id": "file_1"}]}

    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.tool_intercept.get_connector_store",
        lambda: store,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.google_workspace_mcp.drive_search",
        fake_drive_search,
    )

    out = execute_connector_tool(
        "google__drive_search",
        {"query": "name contains 'Roadmap'", "page_size": 5},
        caller_agent_id="chief-of-staff",
    )

    assert json.loads(out or "{}") == {"ok": True, "files": [{"id": "file_1"}]}
    assert captured == {
        "connection_id": connection.id,
        "query": "name contains 'Roadmap'",
        "page_size": 5,
    }


def test_connector_tool_interceptor_rejects_ungranted_google_tool(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    connection = store.upsert_connection(
        provider="google_workspace",
        display_name="ByteDesk Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    provider.bootstrap_services(store, connection, ["drive"])
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.tool_intercept.get_connector_store",
        lambda: store,
    )

    out = execute_connector_tool(
        "google__drive_search",
        {"query": "name contains 'Roadmap'"},
        caller_agent_id="chief-of-staff",
    )

    assert json.loads(out or "{}") == {
        "ok": False,
        "error": "connector_tool_not_granted",
        "namespaced_name": "google__drive_search",
    }


def test_connector_tool_interceptor_rejects_ambiguous_connection(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    first = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace 1",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref-1",
    )
    second = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace 2",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref-2",
    )
    provider.bootstrap_services(store, first, ["drive"])
    provider.bootstrap_services(store, second, ["drive"])
    for connection in (first, second):
        store.upsert_agent_grant(
            connection_id=connection.id,
            agent_id="chief-of-staff",
            service_key="drive",
            tool_key="search",
            enabled=True,
        )
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.tool_intercept.get_connector_store",
        lambda: store,
    )

    out = execute_connector_tool(
        "google__drive_search",
        {"query": "name contains 'Roadmap'"},
        caller_agent_id="chief-of-staff",
    )

    assert json.loads(out or "{}") == {
        "ok": False,
        "error": "connector_tool_connection_ambiguous",
        "namespaced_name": "google__drive_search",
    }
