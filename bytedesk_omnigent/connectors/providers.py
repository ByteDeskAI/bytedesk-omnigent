"""Connector provider Strategy/Adapter implementations.

The connector framework keeps lifecycle, persistence, service toggles, grants,
and admin routes shared. A provider strategy owns only the provider-specific
pieces: authorization, credential normalization, health validation, and agent
tool materialization.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

import httpx
import yaml

from bytedesk_omnigent.connectors.credentials import store_connector_secret
from bytedesk_omnigent.connectors.manifests import (
    ConnectorManifest,
    ConnectorService,
    ConnectorTool,
    atlassian_connector_manifest,
    google_workspace_connector_manifest,
)
from bytedesk_omnigent.connectors.store import (
    ConnectorAgentGrant,
    ConnectorConnection,
    ConnectorServiceState,
    SqlAlchemyConnectorStore,
)
from omnigent.errors import ErrorCode, OmnigentError

_MANAGED_MARKER = "omnigent_connector"
_GOOGLE_WORKSPACE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _metadata_value(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _atlassian_api_token_ref_errors(metadata: dict[str, Any]) -> list[str]:
    required = {
        "base_url_secret": _metadata_value(
            metadata,
            "base_url_secret",
            "baseUrlSecret",
            "site_url_secret",
            "siteUrlSecret",
            "atlassian_base_url_secret",
            "atlassianBaseUrlSecret",
            "jira_base_url_secret",
            "jiraBaseUrlSecret",
            "confluence_base_url_secret",
            "confluenceBaseUrlSecret",
        ),
        "email_secret": _metadata_value(
            metadata,
            "email_secret",
            "emailSecret",
            "atlassian_email_secret",
            "atlassianEmailSecret",
        ),
        "api_token_secret": _metadata_value(
            metadata,
            "api_token_secret",
            "apiTokenSecret",
            "atlassian_api_token_secret",
            "atlassianApiTokenSecret",
        ),
    }
    return [key for key, value in required.items() if not value]


def _is_atlassian_api_token_metadata(metadata: dict[str, Any]) -> bool:
    return _metadata_value(metadata, "auth_mode", "authMode") == "api_token" or bool(
        _metadata_value(
            metadata,
            "base_url_secret",
            "baseUrlSecret",
            "site_url_secret",
            "siteUrlSecret",
            "atlassian_base_url_secret",
            "atlassianBaseUrlSecret",
            "jira_base_url_secret",
            "jiraBaseUrlSecret",
            "confluence_base_url_secret",
            "confluenceBaseUrlSecret",
        )
    )


def google_workspace_token_probe(connection_id: str, scopes: list[str]) -> str:
    """Resolve a Google Workspace access token for a connector connection.

    Kept as a module-level adapter so tests can replace the live Google call
    without touching provider state.
    """

    from bytedesk_omnigent.connectors import google_workspace_mcp

    with google_workspace_mcp.connection_context(connection_id):
        return google_workspace_mcp._token(scopes=scopes)


def _google_live_health_error_metadata(
    connection: ConnectorConnection,
    *,
    required_scopes: list[str],
    auth_mode: str,
) -> dict[str, Any]:
    metadata = connection.metadata
    client_id = _metadata_value(
        metadata,
        "service_account_client_id",
        "serviceAccountClientId",
        "oauth_client_id",
        "oauthClientId",
        "client_id",
        "clientId",
    )
    out: dict[str, Any] = {
        "authMode": auth_mode,
        "requiredScopes": required_scopes,
        "serviceAccountEmail": _metadata_value(
            metadata,
            "service_account_email",
            "serviceAccountEmail",
        ),
    }
    if client_id:
        out["clientId"] = client_id
    return out


@dataclass(frozen=True)
class ConnectorOAuthStartRequest:
    """Generic request to start provider authorization."""

    redirect_uri: str | None = None


@dataclass(frozen=True)
class ConnectorOAuthStartResult:
    """Generic OAuth start response."""

    authorization_url: str
    state: str


@dataclass(frozen=True)
class ConnectorOAuthCallbackRequest:
    """Generic request to finish provider authorization."""

    code: str
    state: str


@dataclass(frozen=True)
class ConnectorCreateRequest:
    """Generic direct-credential connection request."""

    display_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    secret_payload: dict[str, Any] | None = None
    enabled_services: list[str] | None = None


@dataclass(frozen=True)
class ConnectorHealthResult:
    """Provider health-check result normalized for persistence."""

    ok: bool
    status: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConnectorProvider(Protocol):
    """Provider-specific connector behavior (ADR-0008 Strategy + Adapter)."""

    provider: str
    manifest: ConnectorManifest
    supports_oauth: bool
    supports_direct_create: bool

    async def start_oauth(
        self,
        request: ConnectorOAuthStartRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorOAuthStartResult: ...

    async def complete_oauth(
        self,
        request: ConnectorOAuthCallbackRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection: ...

    def create_connection(
        self,
        request: ConnectorCreateRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection: ...

    def check_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult: ...

    def check_live_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult: ...

    def apply_agent_grant(
        self,
        *,
        staging: Path,
        config: dict[str, Any],
        connection: ConnectorConnection,
        services: list[ConnectorServiceState],
        grants: list[ConnectorAgentGrant],
    ) -> None: ...


class BaseConnectorProvider:
    """Common provider behavior shared by every connector adapter."""

    provider: str = ""
    manifest: ConnectorManifest
    supports_oauth = False
    supports_direct_create = False

    async def start_oauth(
        self,
        request: ConnectorOAuthStartRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorOAuthStartResult:
        del request, store
        raise OmnigentError(
            f"{self.provider} does not support OAuth authorization",
            code=ErrorCode.INVALID_INPUT,
        )

    async def complete_oauth(
        self,
        request: ConnectorOAuthCallbackRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection:
        del request, store
        raise OmnigentError(
            f"{self.provider} does not support OAuth authorization",
            code=ErrorCode.INVALID_INPUT,
        )

    def create_connection(
        self,
        request: ConnectorCreateRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection:
        del request, store
        raise OmnigentError(
            f"{self.provider} does not support direct credential registration",
            code=ErrorCode.INVALID_INPUT,
        )

    def check_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult:
        del connection
        if secret_payload is None:
            return ConnectorHealthResult(ok=False, status="error", error="missing_secret")
        return ConnectorHealthResult(ok=True, status="healthy")

    def check_live_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult:
        return self.check_health(connection, secret_payload)

    def apply_agent_grant(
        self,
        *,
        staging: Path,
        config: dict[str, Any],
        connection: ConnectorConnection,
        services: list[ConnectorServiceState],
        grants: list[ConnectorAgentGrant],
    ) -> None:
        del staging, config, connection, services, grants
        raise OmnigentError(
            f"{self.provider} connector cannot materialize agent grants",
            code=ErrorCode.INVALID_INPUT,
        )

    def bootstrap_services(
        self,
        store: SqlAlchemyConnectorStore,
        connection: ConnectorConnection,
        enabled_services: list[str] | None = None,
    ) -> list[ConnectorServiceState]:
        enabled = set(enabled_services or [svc.key for svc in self.manifest.services])
        states: list[ConnectorServiceState] = []
        for svc in self.manifest.services:
            states.append(
                store.upsert_service(
                    connection_id=connection.id,
                    service_key=svc.key,
                    enabled=svc.key in enabled,
                    scopes=svc.scopes,
                )
            )
        return states


def _tools_mapping(config: dict[str, Any]) -> dict[str, Any]:
    tools = config.setdefault("tools", {})
    if not isinstance(tools, dict):
        raise OmnigentError("agent config tools must be a mapping", code=ErrorCode.INVALID_INPUT)
    return tools


def _builtin_list(config: dict[str, Any]) -> list[Any]:
    tools = _tools_mapping(config)
    builtins = tools.setdefault("builtins", [])
    if not isinstance(builtins, list):
        raise OmnigentError(
            "agent config tools.builtins must be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    return builtins


def remove_managed_builtins(config: dict[str, Any], connection_id: str) -> None:
    """Remove connector-managed builtins for one connection before re-applying."""

    tools = config.get("tools")
    if not isinstance(tools, dict):
        return
    builtins = tools.get("builtins")
    if not isinstance(builtins, list):
        return
    tools["builtins"] = [
        entry
        for entry in builtins
        if not (
            isinstance(entry, dict)
            and entry.get("connector_managed") == _MANAGED_MARKER
            and entry.get("connection_id") == connection_id
        )
    ]


def managed_builtin(name: str, connection: ConnectorConnection) -> dict[str, str]:
    """Shared marker shape for connector-managed builtin tools."""

    return {
        "name": name,
        "connection_id": connection.id,
        "connector_provider": connection.provider,
        "connector_managed": _MANAGED_MARKER,
    }


def _safe_mcp_server_name(provider: str, connection_id: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in connection_id).strip("_")
    return f"{provider}_{safe or 'connection'}"


def _service_tool_index(
    manifest: ConnectorManifest,
) -> dict[str, dict[str, ConnectorTool]]:
    return {svc.key: {tool.key: tool for tool in svc.tools} for svc in manifest.services}


def _selected_mcp_tools(
    *,
    manifest: ConnectorManifest,
    services: list[ConnectorServiceState],
    grants: list[ConnectorAgentGrant],
) -> list[str]:
    """Map active agent grants to MCP tool names from the manifest."""

    enabled_services = {svc.service_key for svc in services if svc.enabled}
    tools_by_service = _service_tool_index(manifest)
    selected: list[str] = []
    seen: set[str] = set()
    for grant in grants:
        if not grant.enabled or grant.status != "active":
            continue
        if grant.service_key not in enabled_services:
            continue
        tool = tools_by_service.get(grant.service_key, {}).get(grant.tool_key)
        if tool is None or tool.mcp_tool in seen:
            continue
        selected.append(tool.mcp_tool)
        seen.add(tool.mcp_tool)
    return selected


def _write_stdio_mcp(
    *,
    staging: Path,
    file_stem: str,
    server_name: str,
    module: str,
    connection_id: str,
    tool_allowlist: list[str],
    description: str,
) -> None:
    """Write or remove a connector-managed stdio MCP file."""

    target = staging / "tools" / "mcp" / f"{file_stem}.yaml"
    if not tool_allowlist:
        if target.is_file():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": server_name,
        "transport": "stdio",
        "command": "python",
        "args": ["-m", module, "--connection-id", connection_id],
        "env": {"PYTHONPATH": "/build"},
        "tool_allowlist": tool_allowlist,
        "description": description,
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False))


class AtlassianConnectorProvider(BaseConnectorProvider):
    """Atlassian OAuth adapter for Jira + Confluence."""

    provider = "atlassian"
    manifest = atlassian_connector_manifest()
    supports_oauth = True
    supports_direct_create = True

    async def start_oauth(
        self,
        request: ConnectorOAuthStartRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorOAuthStartResult:
        client_id = _env("OMNIGENT_ATLASSIAN_OAUTH_CLIENT_ID")
        redirect_uri = request.redirect_uri or _env("OMNIGENT_ATLASSIAN_OAUTH_REDIRECT_URI")
        if not client_id or not redirect_uri:
            raise OmnigentError(
                "Atlassian OAuth client id and redirect URI are required",
                code=ErrorCode.INVALID_INPUT,
            )
        state = secrets.token_urlsafe(32)
        store.create_oauth_state(
            state=state,
            provider=self.provider,
            requested_scopes=self.manifest.auth.scopes,
            redirect_uri=redirect_uri,
        )
        params = urlencode(
            {
                "audience": "api.atlassian.com",
                "client_id": client_id,
                "scope": " ".join(self.manifest.auth.scopes),
                "redirect_uri": redirect_uri,
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
        )
        return ConnectorOAuthStartResult(
            authorization_url=f"https://auth.atlassian.com/authorize?{params}",
            state=state,
        )

    async def complete_oauth(
        self,
        request: ConnectorOAuthCallbackRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection:
        oauth_state = store.consume_oauth_state(request.state)
        if oauth_state is None or oauth_state.provider != self.provider:
            raise OmnigentError("Invalid or expired OAuth state", code=ErrorCode.INVALID_INPUT)
        client_id = _env("OMNIGENT_ATLASSIAN_OAUTH_CLIENT_ID")
        client_secret = _env("OMNIGENT_ATLASSIAN_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise OmnigentError(
                "Atlassian OAuth client credentials are required",
                code=ErrorCode.INVALID_INPUT,
            )
        token_payload = await self._exchange_code(
            code=request.code,
            redirect_uri=oauth_state.redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
        )
        resources = token_payload.get("resources") or []
        if not resources:
            raise OmnigentError(
                "Atlassian account returned no cloud resources",
                code=ErrorCode.INVALID_INPUT,
            )
        resource = resources[0]
        connection = store.upsert_connection(
            provider=self.provider,
            display_name=resource.get("name") or resource.get("url") or "Atlassian",
            auth_type=self.manifest.auth.type,
            scopes=list(oauth_state.requested_scopes),
            metadata={
                "cloud_id": resource.get("id"),
                "site_url": resource.get("url"),
                "avatar_url": resource.get("avatarUrl"),
            },
            secret_ref=None,
        )
        secret_ref = store_connector_secret(
            self.provider,
            connection.id,
            {
                **token_payload,
                "cloud_id": resource.get("id"),
                "site_url": resource.get("url"),
            },
        )
        connection = store.upsert_connection(
            provider=self.provider,
            display_name=connection.display_name,
            auth_type=connection.auth_type,
            scopes=connection.scopes,
            metadata=connection.metadata,
            secret_ref=secret_ref,
            connection_id=connection.id,
        )
        self.bootstrap_services(store, connection)
        return connection

    async def _exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            token.raise_for_status()
            payload = token.json()
            resources = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            resources.raise_for_status()
            payload["resources"] = resources.json()
            return payload

    def create_connection(
        self,
        request: ConnectorCreateRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection:
        metadata = dict(request.metadata)
        if request.secret_payload is not None or request.secret_ref is not None:
            raise OmnigentError(
                "Atlassian direct connections store Omnigent secret names in metadata",
                code=ErrorCode.INVALID_INPUT,
            )
        if not _is_atlassian_api_token_metadata(metadata):
            raise OmnigentError(
                "Atlassian direct connections require auth_mode=api_token",
                code=ErrorCode.INVALID_INPUT,
            )
        missing = _atlassian_api_token_ref_errors(metadata)
        if missing:
            raise OmnigentError(
                f"Atlassian direct connection missing metadata: {', '.join(missing)}",
                code=ErrorCode.INVALID_INPUT,
            )
        metadata["auth_mode"] = "api_token"
        connection = store.upsert_connection(
            provider=self.provider,
            display_name=request.display_name or self.manifest.name,
            auth_type=self.manifest.auth.type,
            scopes=self.manifest.auth.scopes,
            metadata=metadata,
            secret_ref=None,
        )
        self.bootstrap_services(store, connection, request.enabled_services)
        return connection

    def check_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult:
        if _is_atlassian_api_token_metadata(connection.metadata):
            missing = _atlassian_api_token_ref_errors(connection.metadata)
            if missing:
                return ConnectorHealthResult(
                    ok=False,
                    status="error",
                    error=f"missing_{missing[0]}",
                    metadata={"authMode": "api_token"},
                )
            return ConnectorHealthResult(
                ok=True,
                status="healthy",
                metadata={"authMode": "api_token"},
            )
        base = super().check_health(connection, secret_payload)
        if not base.ok or secret_payload is None:
            return base
        if not secret_payload.get("access_token"):
            return ConnectorHealthResult(ok=False, status="error", error="missing_access_token")
        if not (connection.metadata.get("cloud_id") or secret_payload.get("cloud_id")):
            return ConnectorHealthResult(ok=False, status="error", error="missing_cloud_id")
        return ConnectorHealthResult(ok=True, status="healthy")

    def apply_agent_grant(
        self,
        *,
        staging: Path,
        config: dict[str, Any],
        connection: ConnectorConnection,
        services: list[ConnectorServiceState],
        grants: list[ConnectorAgentGrant],
    ) -> None:
        remove_managed_builtins(config, connection.id)
        _write_stdio_mcp(
            staging=staging,
            file_stem=f"atlassian-{connection.id}",
            server_name="atlassian",
            module="bytedesk_omnigent.connectors.atlassian_mcp",
            connection_id=connection.id,
            tool_allowlist=_selected_mcp_tools(
                manifest=self.manifest,
                services=services,
                grants=grants,
            ),
            description="Connector-managed Atlassian Jira and Confluence tools.",
        )


class GoogleWorkspaceConnectorProvider(BaseConnectorProvider):
    """Google Workspace domain-wide delegation adapter."""

    provider = "google_workspace"
    manifest = google_workspace_connector_manifest()
    supports_direct_create = True

    def create_connection(
        self,
        request: ConnectorCreateRequest,
        *,
        store: SqlAlchemyConnectorStore,
    ) -> ConnectorConnection:
        delegated_subject = str(request.metadata.get("delegated_subject") or "").strip()
        if not delegated_subject:
            raise OmnigentError(
                "Google Workspace delegated_subject is required",
                code=ErrorCode.INVALID_INPUT,
            )
        secret_payload = request.secret_payload or {}
        has_service_account_json = isinstance(secret_payload.get("service_account_json"), dict)
        has_keyless_runtime = bool(
            request.metadata.get("service_account_email")
            and request.metadata.get("workload_identity_audience")
        )
        if request.secret_ref is None and not has_service_account_json and not has_keyless_runtime:
            raise OmnigentError(
                "Provide service account JSON, a secretRef, or connector-scoped WIF metadata",
                code=ErrorCode.INVALID_INPUT,
            )
        metadata = {
            **request.metadata,
            "domain": request.metadata.get("domain"),
            "delegated_subject": delegated_subject,
        }
        connection = store.upsert_connection(
            provider=self.provider,
            display_name=request.display_name or self.manifest.name,
            auth_type=self.manifest.auth.type,
            scopes=self.manifest.auth.scopes,
            metadata=metadata,
            secret_ref=request.secret_ref,
        )
        if request.secret_payload is not None:
            secret_ref = store_connector_secret(
                self.provider,
                connection.id,
                request.secret_payload,
            )
            connection = store.upsert_connection(
                provider=self.provider,
                display_name=connection.display_name,
                auth_type=connection.auth_type,
                scopes=connection.scopes,
                metadata=connection.metadata,
                secret_ref=secret_ref,
                connection_id=connection.id,
            )
        self.bootstrap_services(store, connection, request.enabled_services)
        return connection

    def check_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult:
        secret_payload = secret_payload or {}
        service_account = secret_payload.get("service_account_json")
        if isinstance(service_account, dict):
            if not service_account.get("client_email"):
                return ConnectorHealthResult(
                    ok=False,
                    status="error",
                    error="missing_service_account_client_email",
                )
            if not service_account.get("private_key"):
                return ConnectorHealthResult(
                    ok=False,
                    status="error",
                    error="missing_service_account_private_key",
                )
            return ConnectorHealthResult(ok=True, status="healthy", metadata={"authMode": "json"})
        metadata = connection.metadata
        service_account_email = str(
            metadata.get("service_account_email")
            or secret_payload.get("service_account_email")
            or ""
        ).strip()
        audience = (
            str(
                metadata.get("workload_identity_audience")
                or secret_payload.get("workload_identity_audience")
                or ""
            ).strip()
        )
        if not service_account_email:
            return ConnectorHealthResult(
                ok=False,
                status="error",
                error="missing_service_account_email",
            )
        if not audience:
            return ConnectorHealthResult(
                ok=False,
                status="error",
                error="missing_workload_identity_audience",
            )
        return ConnectorHealthResult(
            ok=True,
            status="healthy",
            metadata={"authMode": "workload_identity_federation"},
        )

    def check_live_health(
        self,
        connection: ConnectorConnection,
        secret_payload: dict[str, Any] | None,
    ) -> ConnectorHealthResult:
        static = self.check_health(connection, secret_payload)
        if not static.ok:
            return static
        auth_mode = str(static.metadata.get("authMode") or "workload_identity_federation")
        required_scopes = [_GOOGLE_WORKSPACE_DRIVE_SCOPE]
        try:
            google_workspace_token_probe(connection.id, required_scopes)
        except Exception as exc:  # noqa: BLE001
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                detail = str(getattr(response, "text", "") or "")
            if not detail:
                detail = str(exc)
            metadata = _google_live_health_error_metadata(
                connection,
                required_scopes=required_scopes,
                auth_mode=auth_mode,
            )
            if "unauthorized_client" in detail:
                metadata["googleError"] = "unauthorized_client"
                return ConnectorHealthResult(
                    ok=False,
                    status="error",
                    error="domain_wide_delegation_unauthorized",
                    metadata=metadata,
                )
            return ConnectorHealthResult(
                ok=False,
                status="error",
                error="google_workspace_token_probe_failed",
                metadata={**metadata, "detail": detail[:1000]},
            )
        return ConnectorHealthResult(
            ok=True,
            status="healthy",
            metadata={
                **_google_live_health_error_metadata(
                    connection,
                    required_scopes=required_scopes,
                    auth_mode=auth_mode,
                ),
                "probe": "drive_token",
            },
        )

    def apply_agent_grant(
        self,
        *,
        staging: Path,
        config: dict[str, Any],
        connection: ConnectorConnection,
        services: list[ConnectorServiceState],
        grants: list[ConnectorAgentGrant],
    ) -> None:
        del config
        _write_stdio_mcp(
            staging=staging,
            file_stem=f"google-workspace-{connection.id}",
            server_name="google",
            module="bytedesk_omnigent.connectors.google_workspace_mcp",
            connection_id=connection.id,
            tool_allowlist=_selected_mcp_tools(
                manifest=self.manifest,
                services=services,
                grants=grants,
            ),
            description="Connector-managed Google Workspace tools.",
        )


def bytedesk_connector_providers() -> dict[str, type[ConnectorProvider]]:
    """First-party connector providers contributed by the ByteDesk extension."""

    return {
        "atlassian": AtlassianConnectorProvider,
        "google_workspace": GoogleWorkspaceConnectorProvider,
    }


def manifest_services_by_key(manifest: ConnectorManifest) -> dict[str, ConnectorService]:
    return {svc.key: svc for svc in manifest.services}


__all__ = [
    "AtlassianConnectorProvider",
    "BaseConnectorProvider",
    "ConnectorCreateRequest",
    "ConnectorHealthResult",
    "ConnectorOAuthCallbackRequest",
    "ConnectorOAuthStartRequest",
    "ConnectorOAuthStartResult",
    "ConnectorProvider",
    "GoogleWorkspaceConnectorProvider",
    "bytedesk_connector_providers",
    "managed_builtin",
    "manifest_services_by_key",
    "remove_managed_builtins",
]
