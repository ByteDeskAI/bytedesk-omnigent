from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.connectors.manifests import bytedesk_connector_manifests
from bytedesk_omnigent.connectors.providers import (
    AtlassianConnectorProvider,
    ConnectorHealthResult,
    GoogleWorkspaceConnectorProvider,
)
from bytedesk_omnigent.connectors.registry import ConnectorRegistry
from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore
from bytedesk_omnigent.routes.connectors import create_connectors_router
from omnigent.entities import Agent
from omnigent.errors import OmnigentError


class _AuthWithUser:
    def get_user_id(self, request: object) -> str:
        return "u-1"


class _NonAdminStore:
    def is_admin(self, user_id: str) -> bool:
        return False


class _AgentStore:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_by_name(self, name: str) -> Agent | None:
        for agent in self._agents.values():
            if agent.name == name and agent.session_id is None:
                return agent
        return None


def _registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        {m.provider: m for m in bytedesk_connector_manifests()},
        {
            "atlassian": AtlassianConnectorProvider(),
            "google_workspace": GoogleWorkspaceConnectorProvider(),
        },
    )


def _app(auth_provider=None, permission_store=None, agent_store=None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(
        create_connectors_router(
            auth_provider=auth_provider,
            permission_store=permission_store,
            agent_store=agent_store,
        ),
        prefix="/v1",
    )
    return app


def test_catalog_lists_connector_manifests(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)

    resp = TestClient(_app()).get("/v1/connectors/catalog")

    assert resp.status_code == 200, resp.text
    providers = {p["provider"]: p for p in resp.json()["providers"]}
    assert "atlassian" in providers
    assert "google_workspace" in providers
    assert [svc["key"] for svc in providers["atlassian"]["services"]] == [
        "jira",
        "confluence",
    ]


def test_connectors_require_admin(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)
    client = TestClient(
        _app(auth_provider=_AuthWithUser(), permission_store=_NonAdminStore()),
        raise_server_exceptions=False,
    )

    assert client.get("/v1/connectors/catalog").status_code == 403


def test_create_google_workspace_connection_stores_secret_ref(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    stored: dict[str, object] = {}

    def _store_secret(provider: str, connection_id: str, payload: dict) -> str:
        stored["provider"] = provider
        stored["connection_id"] = connection_id
        stored["payload"] = payload
        return "google-secret-ref"

    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.providers.store_connector_secret",
        _store_secret,
    )

    resp = TestClient(_app()).post(
        "/v1/connectors/google_workspace/connections",
        json={
            "displayName": "Acme Workspace",
            "metadata": {
                "delegated_subject": "admin@acme.test",
            },
            "secretPayload": {
                "service_account_json": {"client_email": "svc@acme.test"},
            },
            "enabledServices": ["drive", "gmail"],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["connection"]
    assert body["provider"] == "google_workspace"
    assert body["secretPresent"] is True
    services = {svc["serviceKey"]: svc for svc in body["services"]}
    assert services["drive"]["enabled"] is True
    assert services["gmail"]["enabled"] is True
    assert services["calendar"]["enabled"] is False
    assert stored["provider"] == "google_workspace"


def test_create_atlassian_connection_stores_secret_references(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)

    resp = TestClient(_app()).post(
        "/v1/connectors/atlassian/connections",
        json={
            "displayName": "ByteDesk Atlassian",
            "metadata": {
                "auth_mode": "api_token",
                "base_url_secret": "JIRA_BASE_URL",
                "email_secret": "JIRA_EMAIL",
                "api_token_secret": "JIRA_API_TOKEN",
            },
            "enabledServices": ["jira"],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["connection"]
    assert body["provider"] == "atlassian"
    assert body["secretPresent"] is False
    assert body["metadata"]["base_url_secret"] == "JIRA_BASE_URL"
    services = {svc["serviceKey"]: svc for svc in body["services"]}
    assert services["jira"]["enabled"] is True
    assert services["confluence"]["enabled"] is False


def test_health_check_reports_missing_secret(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="atlassian",
        display_name="Atlassian",
        auth_type="oauth_3lo",
        scopes=[],
        secret_ref="missing",
    )
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.connectors.load_connector_secret",
        lambda ref: None,
    )

    resp = TestClient(_app()).post(f"/v1/connectors/connections/{conn.id}/health-check")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["connection"]["lastHealthStatus"] == "error"


def test_health_check_live_uses_provider_live_probe(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref=None,
    )
    called = {"live": False}

    class _GoogleProvider(GoogleWorkspaceConnectorProvider):
        def check_live_health(self, connection, secret_payload):
            called["live"] = True
            return ConnectorHealthResult(
                ok=False,
                status="error",
                error="domain_wide_delegation_unauthorized",
                metadata={"requiredScopes": ["https://www.googleapis.com/auth/drive"]},
            )

    def registry() -> ConnectorRegistry:
        return ConnectorRegistry(
            {m.provider: m for m in bytedesk_connector_manifests()},
            {
                "atlassian": AtlassianConnectorProvider(),
                "google_workspace": _GoogleProvider(),
            },
        )

    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", registry)

    resp = TestClient(_app()).post(
        f"/v1/connectors/connections/{conn.id}/health-check?live=true"
    )

    assert resp.status_code == 200, resp.text
    assert called["live"] is True
    assert resp.json()["ok"] is False
    assert resp.json()["connection"]["lastHealthStatus"] == "error"
    assert resp.json()["metadata"]["requiredScopes"] == [
        "https://www.googleapis.com/auth/drive"
    ]


def test_grant_endpoint_persists_individual_connector_tools(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    GoogleWorkspaceConnectorProvider().bootstrap_services(store, conn)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)

    resp = TestClient(_app()).post(
        f"/v1/connectors/connections/{conn.id}/agent-grants",
        json={
            "agentId": "ag_maya",
            "tools": ["drive:search", "gmail:search"],
            "materialize": False,
        },
    )

    assert resp.status_code == 200, resp.text
    grants = resp.json()["grants"]
    assert [(g["serviceKey"], g["toolKey"]) for g in grants] == [
        ("drive", "search"),
        ("gmail", "search"),
    ]


def test_grant_endpoint_accepts_agent_name(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    GoogleWorkspaceConnectorProvider().bootstrap_services(store, conn)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)
    agent_store = _AgentStore(
        [
            Agent(
                id="ag_maya",
                created_at=1,
                name="chief-of-staff",
                bundle_location="ag_maya/hash",
            )
        ]
    )

    resp = TestClient(_app(agent_store=agent_store)).post(
        f"/v1/connectors/connections/{conn.id}/agent-grants",
        json={
            "agentId": "chief-of-staff",
            "tools": ["drive:search"],
            "materialize": False,
        },
    )

    assert resp.status_code == 200, resp.text
    grant = resp.json()["grants"][0]
    assert grant["agentId"] == "ag_maya"
    listed = TestClient(_app(agent_store=agent_store)).get(
        "/v1/connectors/agent-grants",
        params={"agentId": "chief-of-staff"},
    )
    assert listed.status_code == 200, listed.text
    assert [row["agentId"] for row in listed.json()["grants"]] == ["ag_maya"]


def test_grant_endpoint_replaces_previous_active_tool_set(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    GoogleWorkspaceConnectorProvider().bootstrap_services(store, conn)
    store.upsert_agent_grant(
        connection_id=conn.id,
        agent_id="ag_maya",
        service_key="gmail",
        tool_key="search",
        enabled=True,
    )
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)

    resp = TestClient(_app()).post(
        f"/v1/connectors/connections/{conn.id}/agent-grants",
        json={
            "agentId": "ag_maya",
            "tools": ["drive:search"],
            "materialize": False,
        },
    )

    assert resp.status_code == 200, resp.text
    grants = {
        (grant.service_key, grant.tool_key): grant
        for grant in store.list_agent_grants(connection_id=conn.id, agent_id="ag_maya")
    }
    assert grants[("drive", "search")].enabled is True
    assert grants[("gmail", "search")].enabled is False


def test_grant_endpoint_rejects_unknown_connector_tool(monkeypatch, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="atlassian",
        display_name="Atlassian",
        auth_type="oauth_3lo",
        scopes=[],
        secret_ref="secret-ref",
    )
    AtlassianConnectorProvider().bootstrap_services(store, conn)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.get_connector_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.routes.connectors.build_connector_registry", _registry)

    resp = TestClient(_app(), raise_server_exceptions=False).post(
        f"/v1/connectors/connections/{conn.id}/agent-grants",
        json={"agentId": "ag_maya", "tools": ["jira:not_real"], "materialize": False},
    )

    assert resp.status_code == 400
