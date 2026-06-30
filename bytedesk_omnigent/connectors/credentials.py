"""Connector credential resolution.

The DB stores only secret references. This module resolves those references at
execution time and adapts provider-specific credential payloads to tool clients.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore, get_connector_store


@dataclass(frozen=True)
class AtlassianCredentials:
    """Resolved Atlassian API base URL and headers for one service."""

    base_url: str
    path_prefix: str
    headers: dict[str, str]


@dataclass(frozen=True)
class GoogleWorkspaceCredentials:
    """Resolved Google Workspace domain-wide delegation credentials.

    ``service_account_json`` is kept for local/dev setups that still use a JSON
    key. ``workload_identity_federation`` is the preferred Omnigent deployment
    path: Kubernetes projects a token, Google STS exchanges it for a short-lived
    Cloud token, IAM Credentials signs the delegated Workspace JWT, and OAuth
    returns the final Workspace access token.
    """

    auth_mode: str
    service_account_email: str
    delegated_subject: str
    scopes: list[str]
    service_account: dict | None = None
    workload_identity_token_file: str | None = None
    workload_identity_token_source: str = "file"
    workload_identity_audience: str | None = None
    kubernetes_token_audience: str | None = None
    kubernetes_token_namespace: str | None = None
    kubernetes_token_service_account: str | None = None
    sts_token_url: str = "https://sts.googleapis.com/v1/token"
    token_uri: str = "https://oauth2.googleapis.com/token"
    domain: str | None = None
    metadata: dict | None = None


def connector_secret_name(provider: str, connection_id: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in connection_id).upper()
    return f"OMNIGENT_CONNECTOR_{provider.upper()}_{safe}"


def store_connector_secret(provider: str, connection_id: str, payload: dict) -> str:
    """Store a connector secret payload and return its secret ref/name."""
    from omnigent.onboarding.secrets import store_secret

    name = connector_secret_name(provider, connection_id)
    store_secret(name, json.dumps(payload, sort_keys=True))
    return name


def load_connector_secret(secret_ref: str | None) -> dict | None:
    """Load a JSON connector secret payload by ref/name."""
    if not secret_ref:
        return None
    from omnigent.onboarding.secrets import load_secret

    raw = load_secret(secret_ref)
    if not raw:
        return None
    return json.loads(raw)


def _payload_value(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _metadata_value(metadata: dict, *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _load_secret_by_ref(secret_ref: str, *, label: str, connection_id: str) -> str:
    from omnigent.onboarding.secrets import load_secret

    value = (load_secret(secret_ref) or "").strip()
    if not value:
        raise KeyError(f"atlassian connector {label} secret missing: {connection_id}")
    return value


def _metadata_has_any(metadata: dict, *keys: str) -> bool:
    return any(_metadata_value(metadata, key) for key in keys)


def _resolve_atlassian_api_token_credentials(
    connection_id: str,
    *,
    service: str,
    metadata: dict,
) -> AtlassianCredentials:
    service_base_url_keys = (
        ("jira_base_url_secret", "jiraBaseUrlSecret")
        if service == "jira"
        else ("confluence_base_url_secret", "confluenceBaseUrlSecret")
    )
    base_url_secret = _metadata_value(
        metadata,
        *service_base_url_keys,
        "base_url_secret",
        "baseUrlSecret",
        "site_url_secret",
        "siteUrlSecret",
        "atlassian_base_url_secret",
        "atlassianBaseUrlSecret",
    )
    email_secret = _metadata_value(
        metadata,
        "email_secret",
        "emailSecret",
        "atlassian_email_secret",
        "atlassianEmailSecret",
    )
    api_token_secret = _metadata_value(
        metadata,
        "api_token_secret",
        "apiTokenSecret",
        "atlassian_api_token_secret",
        "atlassianApiTokenSecret",
    )
    if not base_url_secret:
        raise KeyError(f"atlassian connector base url secret ref missing: {connection_id}")
    if not email_secret:
        raise KeyError(f"atlassian connector email secret ref missing: {connection_id}")
    if not api_token_secret:
        raise KeyError(f"atlassian connector api token secret ref missing: {connection_id}")

    base_url = _load_secret_by_ref(
        base_url_secret,
        label="base url",
        connection_id=connection_id,
    ).rstrip("/")
    if base_url.endswith("/wiki"):
        base_url = base_url[: -len("/wiki")].rstrip("/")
    email = _load_secret_by_ref(
        email_secret,
        label="email",
        connection_id=connection_id,
    )
    api_token = _load_secret_by_ref(
        api_token_secret,
        label="api token",
        connection_id=connection_id,
    )
    basic = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return AtlassianCredentials(
        base_url=base_url,
        path_prefix="",
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )


def google_workspace_kubernetes_token_audience(workload_identity_audience: str | None) -> str:
    """Derive the Kubernetes TokenRequest audience for a Google WIF provider."""

    audience = (workload_identity_audience or "").strip()
    if not audience:
        return ""
    if audience.startswith("//iam.googleapis.com/"):
        return f"https:{audience}"
    return audience


def resolve_atlassian_credentials(
    connection_id: str,
    *,
    service: str,
    store: SqlAlchemyConnectorStore | None = None,
) -> AtlassianCredentials:
    """Resolve connector-owned Atlassian credentials for Jira or Confluence."""
    store = store or get_connector_store()
    connection = store.get_connection(connection_id)
    if connection is None or connection.provider != "atlassian":
        raise KeyError(f"atlassian connector connection not found: {connection_id}")
    metadata = dict(connection.metadata)
    payload = load_connector_secret(connection.secret_ref) or {}
    auth_mode = (
        _metadata_value(metadata, "auth_mode", "authMode")
        or _payload_value(payload, "auth_mode", "authMode")
    )
    if auth_mode == "api_token" or _metadata_has_any(
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
    ):
        return _resolve_atlassian_api_token_credentials(
            connection_id,
            service=service,
            metadata=metadata,
        )

    payload = payload or None
    if payload is None:
        raise KeyError(f"atlassian connector secret missing: {connection_id}")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise KeyError(f"atlassian connector access token missing: {connection_id}")
    cloud_id = str(connection.metadata.get("cloud_id") or payload.get("cloud_id") or "").strip()
    if not cloud_id:
        raise KeyError(f"atlassian connector cloud id missing: {connection_id}")
    return AtlassianCredentials(
        base_url="https://api.atlassian.com",
        path_prefix=f"/ex/{'jira' if service == 'jira' else 'confluence'}/{cloud_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )


def resolve_google_workspace_credentials(
    connection_id: str,
    *,
    store: SqlAlchemyConnectorStore | None = None,
) -> GoogleWorkspaceCredentials:
    """Resolve Google Workspace auth material for one connector connection."""
    store = store or get_connector_store()
    connection = store.get_connection(connection_id)
    if connection is None or connection.provider != "google_workspace":
        raise KeyError(f"google workspace connector connection not found: {connection_id}")
    payload = load_connector_secret(connection.secret_ref) or {}
    service_account = payload.get("service_account_json")
    metadata = dict(connection.metadata)
    delegated_subject = _metadata_value(
        metadata,
        "delegated_subject",
        "subject",
    ) or _payload_value(
        payload,
        "delegated_subject",
        "subject",
    )
    if not delegated_subject:
        raise KeyError(f"google workspace delegated subject missing: {connection_id}")
    if isinstance(service_account, dict):
        service_account_email = str(service_account.get("client_email") or "").strip()
        if not service_account_email:
            raise KeyError(f"google workspace service account email missing: {connection_id}")
        return GoogleWorkspaceCredentials(
            auth_mode="service_account_json",
            service_account_email=service_account_email,
            delegated_subject=delegated_subject,
            scopes=list(connection.scopes),
            service_account=service_account,
            token_uri=str(service_account.get("token_uri") or "https://oauth2.googleapis.com/token"),
            domain=metadata.get("domain"),
            metadata=metadata,
        )

    service_account_email = (
        _metadata_value(metadata, "service_account_email", "serviceAccountEmail")
        or _payload_value(payload, "service_account_email", "serviceAccountEmail")
    )
    if not service_account_email:
        raise KeyError(f"google workspace service account email missing: {connection_id}")
    token_file = (
        _metadata_value(metadata, "workload_identity_token_file", "workloadIdentityTokenFile")
        or _payload_value(payload, "workload_identity_token_file", "workloadIdentityTokenFile")
    )
    audience = (
        _metadata_value(metadata, "workload_identity_audience", "workloadIdentityAudience")
        or _payload_value(payload, "workload_identity_audience", "workloadIdentityAudience")
    )
    if not audience:
        raise KeyError(f"google workspace workload identity audience missing: {connection_id}")
    token_source = (
        _metadata_value(metadata, "workload_identity_token_source", "workloadIdentityTokenSource")
        or _payload_value(payload, "workload_identity_token_source", "workloadIdentityTokenSource")
        or ("file" if token_file else "kubernetes_token_request")
    )
    if token_source not in {"file", "kubernetes_token_request"}:
        raise KeyError(
            f"unsupported google workspace workload identity token source: {token_source}"
        )
    if token_source == "file" and not token_file:
        raise KeyError(f"google workspace workload identity token file missing: {connection_id}")
    kubernetes_token_audience = (
        _metadata_value(metadata, "kubernetes_token_audience", "kubernetesTokenAudience")
        or _payload_value(payload, "kubernetes_token_audience", "kubernetesTokenAudience")
        or google_workspace_kubernetes_token_audience(audience)
    )
    return GoogleWorkspaceCredentials(
        auth_mode="workload_identity_federation",
        service_account_email=service_account_email,
        delegated_subject=delegated_subject,
        scopes=list(connection.scopes),
        workload_identity_token_file=token_file,
        workload_identity_token_source=token_source,
        workload_identity_audience=audience,
        kubernetes_token_audience=kubernetes_token_audience,
        kubernetes_token_namespace=(
            _metadata_value(metadata, "kubernetes_token_namespace", "kubernetesTokenNamespace")
            or _payload_value(payload, "kubernetes_token_namespace", "kubernetesTokenNamespace")
            or None
        ),
        kubernetes_token_service_account=(
            _metadata_value(
                metadata,
                "kubernetes_token_service_account",
                "kubernetesTokenServiceAccount",
            )
            or _payload_value(
                payload,
                "kubernetes_token_service_account",
                "kubernetesTokenServiceAccount",
            )
            or None
        ),
        sts_token_url=(
            _metadata_value(metadata, "sts_token_url", "stsTokenUrl")
            or _payload_value(payload, "sts_token_url", "stsTokenUrl")
            or "https://sts.googleapis.com/v1/token"
        ),
        token_uri=(
            _metadata_value(metadata, "token_uri", "tokenUri")
            or _payload_value(payload, "token_uri", "tokenUri")
            or "https://oauth2.googleapis.com/token"
        ),
        domain=metadata.get("domain"),
        metadata=metadata,
    )
