"""Connector-managed Google Workspace MCP server."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt
from mcp.server.fastmcp import FastMCP

from bytedesk_omnigent.connectors.credentials import resolve_google_workspace_credentials
from bytedesk_omnigent.connectors.manifests import (
    GOOGLE_WORKSPACE_OPERATION_DEFINITIONS,
    GOOGLE_WORKSPACE_SERVICE_CATALOG,
    google_workspace_base_url,
    google_workspace_operation_methods,
    google_workspace_operation_tool_name,
)

mcp = FastMCP("google")
_connection_id: str | None = None
_connection_id_context: ContextVar[str | None] = ContextVar(
    "google_workspace_connector_connection_id",
    default=None,
)
_token_cache: dict[str, tuple[str, int]] = {}
_base_token_cache: dict[str, tuple[str, int]] = {}
_K8S_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_K8S_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
_K8S_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def _connection() -> str:
    connection_id = _connection_id_context.get() or _connection_id
    if not connection_id:
        raise KeyError("missing connector connection id")
    return connection_id


@contextlib.contextmanager
def connection_context(connection_id: str):
    token = _connection_id_context.set(connection_id)
    try:
        yield
    finally:
        _connection_id_context.reset(token)


def _workspace_scopes(credentials, scopes: list[str] | tuple[str, ...] | None = None) -> list[str]:
    scopes = list(scopes or credentials.scopes)
    if not scopes:
        from bytedesk_omnigent.connectors.manifests import google_workspace_connector_manifest

        scopes = google_workspace_connector_manifest().auth.scopes
    return scopes


def _workspace_claims(
    credentials,
    delegated_subject: str,
    now: int,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return {
        "iss": credentials.service_account_email,
        "scope": " ".join(_workspace_scopes(credentials, scopes)),
        "aud": credentials.token_uri,
        "sub": delegated_subject,
        "iat": now,
        "exp": now + 3600,
    }


def _kubernetes_api_base_url() -> str:
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    return f"https://{host}:{port}"


def _read_kubernetes_namespace() -> str:
    if _K8S_NAMESPACE_PATH.is_file():
        return _K8S_NAMESPACE_PATH.read_text().strip()
    return ""


def _decode_kubernetes_identity(token: str) -> tuple[str, str]:
    payload = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    subject = str(payload.get("sub") or "")
    parts = subject.split(":")
    if len(parts) == 4 and parts[0] == "system" and parts[1] == "serviceaccount":
        return parts[2], parts[3]
    return "", ""


def _kubernetes_subject_token(credentials) -> str:
    if not _K8S_TOKEN_PATH.is_file():
        raise KeyError("kubernetes service account token is unavailable")
    base_token = _K8S_TOKEN_PATH.read_text().strip()
    decoded_namespace, decoded_service_account = _decode_kubernetes_identity(base_token)
    namespace = credentials.kubernetes_token_namespace or _read_kubernetes_namespace()
    namespace = namespace or decoded_namespace
    service_account = credentials.kubernetes_token_service_account or decoded_service_account
    audience = credentials.kubernetes_token_audience
    if not namespace:
        raise KeyError("kubernetes namespace is unavailable")
    if not service_account:
        raise KeyError("kubernetes service account name is unavailable")
    if not audience:
        raise KeyError("kubernetes token audience is unavailable")
    verify: str | bool = str(_K8S_CA_PATH) if _K8S_CA_PATH.is_file() else True
    with httpx.Client(verify=verify, timeout=20.0) as client:
        response = client.post(
            f"{_kubernetes_api_base_url()}/api/v1/namespaces/"
            f"{namespace}/serviceaccounts/{service_account}/token",
            headers={
                "Authorization": f"Bearer {base_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenRequest",
                "spec": {
                    "audiences": [audience],
                    "expirationSeconds": 3600,
                },
            },
        )
    response.raise_for_status()
    token = str(response.json().get("status", {}).get("token") or "").strip()
    if not token:
        raise KeyError("kubernetes token request response missing status.token")
    return token


def _workload_identity_subject_token(credentials) -> str:
    source = credentials.workload_identity_token_source
    if source == "file":
        if not credentials.workload_identity_token_file:
            raise KeyError("google workspace workload identity token file missing")
        return Path(credentials.workload_identity_token_file).read_text().strip()
    if source == "kubernetes_token_request":
        return _kubernetes_subject_token(credentials)
    raise KeyError(f"unsupported google workspace workload identity token source: {source}")


def _exchange_assertion(token_uri: str, assertion: str, now: int) -> tuple[str, int]:
    response = httpx.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    access_token = payload["access_token"]
    expires_at = now + int(payload.get("expires_in") or 3600)
    return access_token, expires_at


def _wif_base_token(credentials, now: int) -> tuple[str, int]:
    cache_key = (
        f"{credentials.service_account_email}:"
        f"{credentials.workload_identity_audience}:"
        f"{credentials.workload_identity_token_source}:"
        f"{credentials.workload_identity_token_file or ''}:"
        f"{credentials.kubernetes_token_audience or ''}"
    )
    cached = _base_token_cache.get(cache_key)
    if cached is not None and cached[1] - 60 > now:
        return cached
    subject_token = _workload_identity_subject_token(credentials)
    response = httpx.post(
        credentials.sts_token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": credentials.workload_identity_audience,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "subject_token": subject_token,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    expires_at = now + int(payload.get("expires_in") or 3600)
    _base_token_cache[cache_key] = (token, expires_at)
    return token, expires_at


def _sign_wif_jwt(credentials, claims: dict[str, Any], now: int) -> str:
    base_token, _ = _wif_base_token(credentials, now)
    service_account = quote(credentials.service_account_email, safe="")
    response = httpx.post(
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{service_account}:signJwt",
        headers={
            "Authorization": f"Bearer {base_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"payload": json.dumps(claims, separators=(",", ":"))},
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    signed_jwt = str(payload.get("signedJwt") or "").strip()
    if not signed_jwt:
        raise KeyError("google workspace signJwt response missing signedJwt")
    return signed_jwt


def _token(
    subject: str | None = None,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> str:
    now = int(time.time())
    credentials = resolve_google_workspace_credentials(_connection())
    delegated_subject = subject or credentials.delegated_subject
    requested_scopes = tuple(_workspace_scopes(credentials, scopes))
    cache_key = f"{credentials.auth_mode}:{delegated_subject}:{' '.join(requested_scopes)}"
    cached = _token_cache.get(cache_key)
    if cached is not None and cached[1] - 60 > now:
        return cached[0]
    claims = _workspace_claims(credentials, delegated_subject, now, scopes=requested_scopes)
    if credentials.auth_mode == "service_account_json":
        if credentials.service_account is None:
            raise KeyError("google workspace service account JSON missing")
        assertion = jwt.encode(
            claims,
            credentials.service_account["private_key"],
            algorithm="RS256",
        )
    elif credentials.auth_mode == "workload_identity_federation":
        assertion = _sign_wif_jwt(credentials, claims, now)
    else:
        raise KeyError(f"unsupported google workspace auth mode: {credentials.auth_mode}")
    access_token, expires_at = _exchange_assertion(credentials.token_uri, assertion, now)
    _token_cache[cache_key] = (access_token, expires_at)
    return access_token


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if v is not None}


def _request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    subject: str | None = None,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    try:
        response = httpx.request(
            method,
            url,
            params=_clean_params(params),
            json=json_body,
            headers={
                "Authorization": f"Bearer {_token(subject, scopes=scopes)}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        response.raise_for_status()
    except KeyError as exc:
        return {"ok": False, "error": "google_workspace_not_configured", "detail": str(exc)}
    except httpx.HTTPStatusError as exc:
        return _http_error_result(exc, scopes=scopes)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "error": "google_workspace_request_failed",
            "detail": type(exc).__name__,
        }
    if response.content:
        with contextlib.suppress(ValueError):
            return {"ok": True, "data": response.json()}
        return {"ok": True, "text": response.text}
    return {"ok": True, "data": {}}


def _http_error_result(
    exc: httpx.HTTPStatusError,
    *,
    scopes: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    body = exc.response.text[:1000]
    google_error = ""
    with contextlib.suppress(ValueError):
        payload = exc.response.json()
        if isinstance(payload, dict):
            google_error = str(payload.get("error") or "")
    if exc.response.status_code == 401 and (
        google_error == "unauthorized_client" or "unauthorized_client" in body
    ):
        return {
            "ok": False,
            "error": "domain_wide_delegation_unauthorized",
            "status": exc.response.status_code,
            "googleError": google_error or "unauthorized_client",
            "requiredScopes": list(scopes or []),
        }
    return {
        "ok": False,
        "error": "google_workspace_http_error",
        "status": exc.response.status_code,
        "body": body,
    }


def _service_key(service_id: str) -> str:
    return service_id.replace("-", "_")


def _service_scopes(service_key: str) -> list[str]:
    return list(GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]["scopes"])


def _data(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    data = result.get("data")
    return data if isinstance(data, dict) else {"value": data}


def _message_raw(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")


def _service_operation_url(service_key: str, path: str) -> str:
    if path.startswith("https://"):
        host = urlsplit(path).netloc
        if host == "www.googleapis.com" or host.endswith(".googleapis.com"):
            return path
        raise ValueError("Only googleapis.com URLs are supported")
    base = google_workspace_base_url(service_key)
    if not base:
        raise ValueError(f"Google Workspace service has no API base URL: {service_key}")
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _service_operation_call(
    *,
    service_key: str,
    operation_key: str,
    path: str,
    method: str | None = None,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    subject: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if service_key not in GOOGLE_WORKSPACE_SERVICE_CATALOG:
        return {"ok": False, "error": "unknown_google_workspace_service", "serviceId": service_key}
    allowed_methods = google_workspace_operation_methods(operation_key)
    resolved_method = (method or allowed_methods[0]).upper()
    if resolved_method not in allowed_methods:
        return {
            "ok": False,
            "error": "unsupported_google_workspace_operation_method",
            "serviceId": service_key,
            "operation": operation_key,
            "method": resolved_method,
            "allowedMethods": allowed_methods,
        }
    try:
        url = _service_operation_url(service_key, path)
    except ValueError as exc:
        return {
            "ok": False,
            "error": "invalid_google_workspace_operation",
            "detail": str(exc),
        }
    planned = {
        "serviceId": service_key,
        "googleServiceId": GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]["service_id"],
        "operation": operation_key,
        "method": resolved_method,
        "url": url,
        "query": query or {},
        "body": body or {},
    }
    if dry_run:
        return {"ok": True, "planned": planned}
    result = _request(
        resolved_method,
        url,
        params=query,
        json_body=body,
        subject=subject,
        scopes=_service_scopes(service_key),
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "operation": planned, "data": data}


def _make_service_operation_tool(
    service_key: str,
    operation_key: str,
) -> Callable[..., dict[str, Any]]:
    service_name = str(GOOGLE_WORKSPACE_SERVICE_CATALOG[service_key]["name"])
    operation_label = GOOGLE_WORKSPACE_OPERATION_DEFINITIONS[operation_key][0]

    def service_operation_tool(
        path: str,
        method: str | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        subject: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return _service_operation_call(
            service_key=service_key,
            operation_key=operation_key,
            path=path,
            method=method,
            query=query,
            body=body,
            subject=subject,
            dry_run=dry_run,
        )

    service_operation_tool.__name__ = google_workspace_operation_tool_name(
        service_key,
        operation_key,
    )
    service_operation_tool.__doc__ = (
        f"{operation_label} {service_name} through the structured Google Workspace "
        f"{service_key}:{operation_key} connector action."
    )
    return service_operation_tool


def _register_service_operation_tools() -> None:
    for service_key, spec in GOOGLE_WORKSPACE_SERVICE_CATALOG.items():
        for operation_key in spec["operations"]:
            tool_name = google_workspace_operation_tool_name(service_key, operation_key)
            if tool_name in globals():
                continue
            fn = _make_service_operation_tool(service_key, operation_key)
            globals()[tool_name] = fn
            mcp.add_tool(fn, name=tool_name, description=fn.__doc__)


@mcp.tool()
def services_list(include_operations: bool = False) -> dict[str, Any]:
    from bytedesk_omnigent.connectors.manifests import google_workspace_connector_manifest

    manifest = google_workspace_connector_manifest()
    services: list[dict[str, Any]] = []
    for service in manifest.services:
        entry: dict[str, Any] = {
            "key": service.key,
            "name": service.name,
            "description": service.description,
            "scopes": list(service.scopes),
        }
        if include_operations:
            entry["tools"] = [tool.to_dict() for tool in service.tools]
        services.append(entry)
    return {"ok": True, "services": services}


@mcp.tool()
def capabilities_get(service_id: str) -> dict[str, Any]:
    from bytedesk_omnigent.connectors.manifests import google_workspace_connector_manifest

    service_key = _service_key(service_id)
    service = google_workspace_connector_manifest().service(service_key)
    if service is None:
        return {"ok": False, "error": "unknown_google_workspace_service", "serviceId": service_id}
    return {"ok": True, "service": service.to_dict()}


@mcp.tool()
def subject_resolve(subject: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    del agent_id
    if subject:
        return {"ok": True, "subject": subject}
    try:
        credentials = resolve_google_workspace_credentials(_connection())
    except KeyError as exc:
        return {"ok": False, "error": "google_workspace_not_configured", "detail": str(exc)}
    return {"ok": True, "subject": credentials.delegated_subject}


@mcp.tool()
def audit_query(
    limit: int = 50,
    service_id: str | None = None,
    subject: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "rows": [],
        "warning": "connector_audit_persistence_not_enabled",
        "filters": {
            "limit": max(1, min(int(limit), 200)),
            "serviceId": service_id,
            "subject": subject,
            "operation": operation,
        },
    }


@mcp.tool()
def drive_search(query: str = "trashed=false", page_size: int = 10) -> dict[str, Any]:
    result = _request(
        "GET",
        "https://www.googleapis.com/drive/v3/files",
        params={
            "q": query or "trashed=false",
            "pageSize": max(1, min(int(page_size), 100)),
            "fields": "files(id,name,mimeType,webViewLink,modifiedTime),nextPageToken",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        scopes=_service_scopes("drive"),
    )
    data = _data(result)
    if not result.get("ok"):
        return data
    return {"ok": True, "files": data.get("files", []), "nextPageToken": data.get("nextPageToken")}


@mcp.tool()
def drive_file_create(
    name: str, mime_type: str | None = None, folder_id: str | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if mime_type:
        metadata["mimeType"] = mime_type
    if folder_id:
        metadata["parents"] = [folder_id]
    result = _request(
        "POST",
        "https://www.googleapis.com/drive/v3/files",
        params={"fields": "id,name,mimeType,webViewLink"},
        json_body=metadata,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "file": data}


@mcp.tool()
def drive_share_internal(
    file_id: str, role: str = "reader", domain: str | None = None
) -> dict[str, Any]:
    try:
        credentials = resolve_google_workspace_credentials(_connection())
    except KeyError as exc:
        return {"ok": False, "error": "google_workspace_not_configured", "detail": str(exc)}
    target_domain = domain or credentials.domain
    if not target_domain:
        return {"ok": False, "error": "missing_domain"}
    result = _request(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        params={"sendNotificationEmail": "false"},
        json_body={"type": "domain", "role": role, "domain": target_domain},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "permission": data}


@mcp.tool()
def drive_replicate_template(
    template_file_id: str,
    name: str,
    folder_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if folder_id:
        body["parents"] = [folder_id]
    result = _request(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{template_file_id}/copy",
        params={"fields": "id,name,mimeType,webViewLink"},
        json_body=body,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "file": data}


@mcp.tool()
def drive_file_copy(
    file_id: str,
    name: str | None = None,
    folder_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = dict(metadata or {})
    if name:
        body["name"] = name
    if folder_id:
        body["parents"] = [folder_id]
    result = _request(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{quote(file_id)}/copy",
        params={"supportsAllDrives": "true", "fields": "id,name,mimeType,webViewLink,parents"},
        json_body=body,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "file": data}


@mcp.tool()
def docs_create(title: str) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://docs.googleapis.com/v1/documents",
        json_body={"title": title},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "document": data}


@mcp.tool()
def docs_batch_update(document_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
    result = _request(
        "POST",
        f"https://docs.googleapis.com/v1/documents/{document_id}:batchUpdate",
        json_body={"requests": requests},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "replies": data.get("replies", [])}


@mcp.tool()
def docs_template_merge(
    template_file_id: str,
    name: str,
    replacements: dict[str, str],
    folder_id: str | None = None,
) -> dict[str, Any]:
    copied = drive_replicate_template(template_file_id, name, folder_id)
    if not copied.get("ok"):
        return copied
    document_id = (copied.get("file") or {}).get("id")
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": token, "matchCase": True},
                "replaceText": value,
            }
        }
        for token, value in replacements.items()
    ]
    if requests and document_id:
        updated = docs_batch_update(document_id, requests)
        if not updated.get("ok"):
            return updated
    return {"ok": True, "document": copied.get("file")}


@mcp.tool()
def docs_template_seed(title: str, body: str = "") -> dict[str, Any]:
    created = docs_create(title)
    document_id = (created.get("document") or {}).get("documentId")
    if body and document_id:
        updated = docs_batch_update(
            document_id,
            [{"insertText": {"location": {"index": 1}, "text": body}}],
        )
        if not updated.get("ok"):
            return updated
    return created


@mcp.tool()
def docs_templates_list() -> dict[str, Any]:
    try:
        credentials = resolve_google_workspace_credentials(_connection())
    except KeyError as exc:
        return {"ok": False, "error": "google_workspace_not_configured", "detail": str(exc)}
    metadata = credentials.metadata or {}
    return {"ok": True, "templates": metadata.get("document_templates", {})}


@mcp.tool()
def sheets_create(title: str) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://sheets.googleapis.com/v4/spreadsheets",
        json_body={"properties": {"title": title}},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "spreadsheet": data}


@mcp.tool()
def sheets_values_update(
    spreadsheet_id: str,
    range_name: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> dict[str, Any]:
    result = _request(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_name}",
        params={"valueInputOption": value_input_option},
        json_body={"values": values},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "update": data}


@mcp.tool()
def slides_create(title: str) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://slides.googleapis.com/v1/presentations",
        json_body={"title": title},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "presentation": data}


@mcp.tool()
def forms_create(title: str) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://forms.googleapis.com/v1/forms",
        json_body={"info": {"title": title}},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "form": data}


@mcp.tool()
def gmail_search(query: str, max_results: int = 10) -> dict[str, Any]:
    result = _request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params={"q": query, "maxResults": max(1, min(int(max_results), 100))},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "messages": data.get("messages", [])}


@mcp.tool()
def gmail_thread_read(thread_id: str, format: str = "metadata") -> dict[str, Any]:
    result = _request(
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
        params={"format": format},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "thread": data}


@mcp.tool()
def gmail_draft_create(to: str, subject: str, body: str) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        json_body={"message": {"raw": _message_raw(to, subject, body)}},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "draft": data}


@mcp.tool()
def gmail_send_internal(
    raw_message_base64url: str = "",
    to: str = "",
    subject: str = "",
    body: str = "",
) -> dict[str, Any]:
    raw = raw_message_base64url or (_message_raw(to, subject, body) if to else "")
    if not raw:
        return {"ok": False, "error": "missing_message"}
    result = _request(
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        json_body={"raw": raw},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "message": data}


@mcp.tool()
def calendar_freebusy(time_min: str, time_max: str, calendar_ids: list[str]) -> dict[str, Any]:
    result = _request(
        "POST",
        "https://www.googleapis.com/calendar/v3/freeBusy",
        json_body={
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": calendar_id} for calendar_id in calendar_ids],
        },
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "freebusy": data}


@mcp.tool()
def calendar_event_create(
    summary: str,
    start: dict[str, Any],
    end: dict[str, Any],
    calendar_id: str = "primary",
    description: str | None = None,
    attendees: list[dict[str, str]] | None = None,
    conference: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"summary": summary, "start": start, "end": end}
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = attendees
    params: dict[str, Any] = {}
    if conference:
        params["conferenceDataVersion"] = 1
        body["conferenceData"] = {
            "createRequest": {"requestId": f"omnigent-{int(time.time() * 1000)}"}
        }
    result = _request(
        "POST",
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        params=params,
        json_body=body,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "event": data}


@mcp.tool()
def meeting_schedule(
    summary: str,
    start: dict[str, Any],
    end: dict[str, Any],
    calendar_id: str = "primary",
    attendees: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return calendar_event_create(
        summary=summary,
        start=start,
        end=end,
        calendar_id=calendar_id,
        attendees=attendees,
        conference=True,
    )


@mcp.tool()
def meet_space_create() -> dict[str, Any]:
    result = _request("POST", "https://meet.googleapis.com/v2/spaces", json_body={})
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "space": data}


@mcp.tool()
def chat_send_internal(space: str, text: str) -> dict[str, Any]:
    result = _request(
        "POST",
        f"https://chat.googleapis.com/v1/{space}/messages",
        json_body={"text": text},
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "message": data}


@mcp.tool()
def people_search(query: str, page_size: int = 10) -> dict[str, Any]:
    result = _request(
        "GET",
        "https://people.googleapis.com/v1/people:searchContacts",
        params={
            "query": query,
            "pageSize": max(1, min(int(page_size), 30)),
            "readMask": "names,emailAddresses,organizations,phoneNumbers",
        },
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "results": data.get("results", [])}


@mcp.tool()
def directory_user_get(user_key: str) -> dict[str, Any]:
    result = _request(
        "GET",
        f"https://admin.googleapis.com/admin/directory/v1/users/{user_key}",
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "user": data}


@mcp.tool()
def tasks_create(
    title: str,
    notes: str | None = None,
    tasklist: str = "@default",
) -> dict[str, Any]:
    body = {"title": title}
    if notes:
        body["notes"] = notes
    result = _request(
        "POST",
        f"https://tasks.googleapis.com/tasks/v1/lists/{quote(tasklist)}/tasks",
        json_body=body,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "task": data}


@mcp.tool()
def keep_note_create(title: str, text: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"title": title}
    if text:
        body["body"] = {"text": {"text": text}}
    result = _request(
        "POST",
        "https://keep.googleapis.com/v1/notes",
        json_body=body,
    )
    data = _data(result)
    return data if not result.get("ok") else {"ok": True, "note": data}


_register_service_operation_tools()


def main() -> None:
    global _connection_id
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    args = parser.parse_args()
    _connection_id = args.connection_id
    mcp.run("stdio")


if __name__ == "__main__":
    main()
