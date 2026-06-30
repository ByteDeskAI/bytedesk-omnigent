from __future__ import annotations

import yaml

from bytedesk_omnigent.connectors.credentials import resolve_google_workspace_credentials
from bytedesk_omnigent.connectors.manifests import (
    GOOGLE_WORKSPACE_SERVICE_CATALOG,
    bytedesk_connector_manifests,
)
from bytedesk_omnigent.connectors.providers import (
    AtlassianConnectorProvider,
    ConnectorCreateRequest,
    GoogleWorkspaceConnectorProvider,
)
from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore
from bytedesk_omnigent.extension import BytedeskExtension

GOOGLE_WORKSPACE_MCP_TOOLS = {
    "services_list",
    "capabilities_get",
    "subject_resolve",
    "audit_query",
    "docs_create",
    "docs_batch_update",
    "docs_template_merge",
    "docs_template_seed",
    "docs_templates_list",
    "sheets_create",
    "sheets_values_update",
    "slides_create",
    "forms_create",
    "drive_share_internal",
    "drive_replicate_template",
    "drive_search",
    "drive_file_create",
    "drive_file_copy",
    "gmail_draft_create",
    "gmail_thread_read",
    "gmail_search",
    "gmail_send_internal",
    "calendar_event_create",
    "calendar_freebusy",
    "meet_space_create",
    "meeting_schedule",
    "chat_send_internal",
    "people_search",
    "directory_user_get",
    "tasks_create",
    "keep_note_create",
}


def test_bytedesk_extension_contributes_connector_manifests() -> None:
    providers = {m.provider for m in BytedeskExtension().connector_manifests()}
    assert providers == {"atlassian", "google_workspace"}


def test_bytedesk_extension_contributes_connector_provider_strategies() -> None:
    providers = BytedeskExtension().connector_providers()
    assert set(providers) == {"atlassian", "google_workspace"}
    assert providers["google_workspace"]().provider == "google_workspace"


def test_connector_manifests_cover_v1_services() -> None:
    by_provider = {m.provider: m for m in bytedesk_connector_manifests()}
    assert [svc.key for svc in by_provider["atlassian"].services] == [
        "jira",
        "confluence",
    ]
    jira_tools = {
        tool.mcp_tool
        for svc in by_provider["atlassian"].services
        if svc.key == "jira"
        for tool in svc.tools
    }
    assert jira_tools == {
        "jira_search",
        "jira_get_issue",
        "jira_add_comment",
        "jira_transition",
        "jira_create_issue",
    }
    setup_fields = {
        field.key: field for field in by_provider["atlassian"].auth.setup_fields
    }
    assert setup_fields["auth_mode"].target == "metadata"
    assert setup_fields["auth_mode"].required is False
    assert setup_fields["base_url_secret"].target == "metadata"
    assert setup_fields["email_secret"].target == "metadata"
    assert setup_fields["api_token_secret"].target == "metadata"
    assert all(field.input == "text" for field in setup_fields.values())

    setup_fields = {
        field.key: field for field in by_provider["google_workspace"].auth.setup_fields
    }
    assert setup_fields["delegated_subject"].target == "metadata"
    assert setup_fields["service_account_json"].target == "secret_payload"
    assert setup_fields["service_account_json"].required is False
    assert setup_fields["service_account_email"].target == "metadata"
    assert {svc.key for svc in by_provider["google_workspace"].services} == set(
        GOOGLE_WORKSPACE_SERVICE_CATALOG
    )
    google_tools = {
        tool.mcp_tool for svc in by_provider["google_workspace"].services for tool in svc.tools
    }
    assert google_tools >= GOOGLE_WORKSPACE_MCP_TOOLS
    assert "api_call" not in google_tools
    assert "sites_read" in google_tools
    assert "vault_admin_mutate" in google_tools
    assert "vertex_ai_generate" in google_tools


def test_connector_store_upserts_connection_services_and_grants(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    conn = store.upsert_connection(
        provider="atlassian",
        display_name="ByteDesk",
        auth_type="oauth_3lo",
        scopes=["read:jira-work"],
        metadata={"cloud_id": "cloud-1"},
        secret_ref="secret-ref",
    )

    svc = store.upsert_service(
        connection_id=conn.id,
        service_key="jira",
        enabled=True,
        scopes=["read:jira-work"],
    )
    grant = store.upsert_agent_grant(
        connection_id=conn.id,
        agent_id="ag_1",
        service_key="jira",
        tool_key="search",
        enabled=True,
    )

    assert store.get_connection(conn.id).metadata["cloud_id"] == "cloud-1"
    assert store.list_services(conn.id) == [svc]
    assert store.list_agent_grants(connection_id=conn.id) == [grant]


def test_connector_provider_creates_connection_through_shared_store(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.providers.store_connector_secret",
        lambda provider, connection_id, payload: f"{provider}:{connection_id}",
    )

    conn = GoogleWorkspaceConnectorProvider().create_connection(
        ConnectorCreateRequest(
            display_name="Acme Workspace",
            metadata={"delegated_subject": "admin@acme.test"},
            secret_payload={"service_account_json": {"client_email": "svc@acme.test"}},
            enabled_services=["drive"],
        ),
        store=store,
    )

    services = {svc.service_key: svc for svc in store.list_services(conn.id)}
    assert conn.provider == "google_workspace"
    assert services["drive"].enabled is True
    assert services["gmail"].enabled is False


def test_atlassian_provider_creates_api_token_reference_connection(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)

    conn = AtlassianConnectorProvider().create_connection(
        ConnectorCreateRequest(
            display_name="ByteDesk Atlassian",
            metadata={
                "auth_mode": "api_token",
                "base_url_secret": "JIRA_BASE_URL",
                "email_secret": "JIRA_EMAIL",
                "api_token_secret": "JIRA_API_TOKEN",
            },
            enabled_services=["jira"],
        ),
        store=store,
    )

    services = {svc.service_key: svc for svc in store.list_services(conn.id)}
    assert conn.provider == "atlassian"
    assert conn.secret_ref is None
    assert conn.metadata["base_url_secret"] == "JIRA_BASE_URL"
    assert services["jira"].enabled is True
    assert services["confluence"].enabled is False


def test_google_workspace_provider_creates_keyless_wif_connection(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)

    conn = GoogleWorkspaceConnectorProvider().create_connection(
        ConnectorCreateRequest(
            display_name="ByteDesk Workspace",
            metadata={
                "delegated_subject": "admin@bytedesk.test",
                "service_account_email": "workspace-agents@project.iam.gserviceaccount.com",
                "workload_identity_token_file": "/var/run/secrets/google-workspace/token",
                "workload_identity_audience": (
                    "//iam.googleapis.com/projects/1/pools/p/providers/k8s"
                ),
            },
            enabled_services=["drive"],
        ),
        store=store,
    )

    assert conn.secret_ref is None
    assert conn.metadata["service_account_email"] == (
        "workspace-agents@project.iam.gserviceaccount.com"
    )
    assert resolve_google_workspace_credentials(conn.id, store=store).auth_mode == (
        "workload_identity_federation"
    )


def test_google_workspace_provider_creates_kubernetes_token_request_connection(
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)

    conn = GoogleWorkspaceConnectorProvider().create_connection(
        ConnectorCreateRequest(
            display_name="ByteDesk Workspace",
            metadata={
                "delegated_subject": "admin@bytedesk.test",
                "service_account_email": "workspace-agents@project.iam.gserviceaccount.com",
                "workload_identity_audience": (
                    "//iam.googleapis.com/projects/1/pools/p/providers/k8s"
                ),
            },
            enabled_services=["drive"],
        ),
        store=store,
    )
    credentials = resolve_google_workspace_credentials(conn.id, store=store)

    assert credentials.auth_mode == "workload_identity_federation"
    assert credentials.workload_identity_token_source == "kubernetes_token_request"
    assert not credentials.workload_identity_token_file
    assert credentials.kubernetes_token_audience == (
        "https://iam.googleapis.com/projects/1/pools/p/providers/k8s"
    )


def test_google_workspace_health_accepts_keyless_wif_metadata(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="ByteDesk Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={
            "delegated_subject": "admin@bytedesk.test",
            "service_account_email": "workspace-agents@project.iam.gserviceaccount.com",
            "workload_identity_token_file": "/var/run/secrets/google-workspace/token",
            "workload_identity_audience": "//iam.googleapis.com/projects/1/pools/p/providers/k8s",
        },
        secret_ref=None,
    )

    health = provider.check_health(conn, None)

    assert health.ok is True
    assert health.status == "healthy"


def test_google_workspace_live_health_reports_domain_wide_delegation_gap(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="ByteDesk Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={
            "delegated_subject": "admin@bytedesk.test",
            "service_account_email": "workspace-agents@project.iam.gserviceaccount.com",
            "service_account_client_id": "123456789",
            "workload_identity_token_file": "/var/run/secrets/google-workspace/token",
            "workload_identity_audience": "//iam.googleapis.com/projects/1/pools/p/providers/k8s",
        },
        secret_ref=None,
    )

    def _fake_token(*args, **kwargs):
        raise RuntimeError(
            '{"error":"unauthorized_client","error_description":"Client is unauthorized"}'
        )

    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.providers.google_workspace_token_probe",
        _fake_token,
    )

    health = provider.check_live_health(conn, None)

    assert health.ok is False
    assert health.status == "error"
    assert health.error == "domain_wide_delegation_unauthorized"
    assert health.metadata["authMode"] == "workload_identity_federation"
    assert health.metadata["clientId"] == "123456789"
    assert health.metadata["requiredScopes"] == ["https://www.googleapis.com/auth/drive"]


def test_atlassian_health_accepts_api_token_secret_references(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = AtlassianConnectorProvider()
    conn = store.upsert_connection(
        provider="atlassian",
        display_name="ByteDesk Atlassian",
        auth_type="oauth_3lo",
        scopes=[],
        metadata={
            "auth_mode": "api_token",
            "base_url_secret": "JIRA_BASE_URL",
            "email_secret": "JIRA_EMAIL",
            "api_token_secret": "JIRA_API_TOKEN",
        },
        secret_ref=None,
    )

    health = provider.check_health(conn, None)

    assert health.ok is True
    assert health.status == "healthy"
    assert health.metadata["authMode"] == "api_token"


def test_atlassian_grant_materializes_mcp_tool_allowlist(tmp_path, db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = AtlassianConnectorProvider()
    conn = store.upsert_connection(
        provider="atlassian",
        display_name="ByteDesk",
        auth_type="oauth_3lo",
        scopes=[],
        metadata={"cloud_id": "cloud-1"},
        secret_ref="secret-ref",
    )
    services = provider.bootstrap_services(store, conn)
    grants = [
        store.upsert_agent_grant(
            connection_id=conn.id,
            agent_id="ag_maya",
            service_key="jira",
            tool_key="search",
            enabled=True,
        ),
        store.upsert_agent_grant(
            connection_id=conn.id,
            agent_id="ag_maya",
            service_key="confluence",
            tool_key="get_page",
            enabled=True,
        ),
    ]

    provider.apply_agent_grant(
        staging=tmp_path,
        config={},
        connection=conn,
        services=services,
        grants=grants,
    )

    payload = yaml.safe_load((tmp_path / "tools/mcp" / f"atlassian-{conn.id}.yaml").read_text())
    assert payload["name"] == "atlassian"
    assert payload["args"] == [
        "-m",
        "bytedesk_omnigent.connectors.atlassian_mcp",
        "--connection-id",
        conn.id,
    ]
    assert payload["tool_allowlist"] == ["jira_search", "confluence_get_page"]


def test_maya_google_workspace_grant_materializes_all_action_allowlists(
    tmp_path,
    db_uri: str,
) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    provider = GoogleWorkspaceConnectorProvider()
    conn = store.upsert_connection(
        provider="google_workspace",
        display_name="ByteDesk Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    services = provider.bootstrap_services(store, conn)
    for service in provider.manifest.services:
        for tool in service.tools:
            store.upsert_agent_grant(
                connection_id=conn.id,
                agent_id="ag_maya",
                service_key=service.key,
                tool_key=tool.key,
                enabled=True,
            )

    provider.apply_agent_grant(
        staging=tmp_path,
        config={},
        connection=conn,
        services=services,
        grants=store.list_agent_grants(connection_id=conn.id, agent_id="ag_maya"),
    )

    payload = yaml.safe_load(
        (tmp_path / "tools/mcp" / f"google-workspace-{conn.id}.yaml").read_text()
    )
    assert payload["name"] == "google"
    assert payload["args"] == [
        "-m",
        "bytedesk_omnigent.connectors.google_workspace_mcp",
        "--connection-id",
        conn.id,
    ]
    assert set(payload["tool_allowlist"]) == {
        tool.mcp_tool for service in provider.manifest.services for tool in service.tools
    }
    assert "drive_search" in payload["tool_allowlist"]


def test_oauth_state_is_single_use(db_uri: str) -> None:
    store = SqlAlchemyConnectorStore(db_uri)
    store.create_oauth_state(
        state="opaque-state",
        provider="atlassian",
        requested_scopes=["read:jira-work"],
        redirect_uri="https://omnigent.test/callback",
        now=100,
    )

    consumed = store.consume_oauth_state("opaque-state", now=101)
    assert consumed is not None
    assert consumed.provider == "atlassian"
    assert store.consume_oauth_state("opaque-state", now=102) is None
