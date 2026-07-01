from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx

from bytedesk_omnigent.connectors import google_workspace_mcp
from bytedesk_omnigent.connectors.credentials import GoogleWorkspaceCredentials
from bytedesk_omnigent.connectors.manifests import google_workspace_connector_manifest


def test_google_workspace_manifest_tools_have_mcp_implementations() -> None:
    manifest = google_workspace_connector_manifest()

    missing = [
        tool.mcp_tool
        for service in manifest.services
        for tool in service.tools
        if not callable(getattr(google_workspace_mcp, tool.mcp_tool, None))
    ]

    assert missing == []


def test_google_workspace_services_list_exposes_all_catalog_actions() -> None:
    out = google_workspace_mcp.services_list(include_operations=True)

    tools = {
        tool["mcpTool"]
        for service in out["services"]
        for tool in service.get("tools", [])
    }
    manifest_tools = {
        tool.mcp_tool
        for service in google_workspace_connector_manifest().services
        for tool in service.tools
    }
    assert out["ok"] is True
    assert tools == manifest_tools


def test_google_workspace_manifest_does_not_expose_generic_api_call() -> None:
    tools = {
        tool.mcp_tool
        for service in google_workspace_connector_manifest().services
        for tool in service.tools
    }

    assert "api_call" not in tools
    assert not hasattr(google_workspace_mcp, "api_call")


def test_google_workspace_structured_operation_tool_routes_to_google_api(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "url": url, **kwargs})
        return {"ok": True, "data": {"id": "form_1"}}

    monkeypatch.setattr(google_workspace_mcp, "_request", fake_request)

    out = google_workspace_mcp.forms_read(
        path="/v1/forms/form_1",
    )

    assert out["ok"] is True
    assert out["data"] == {"id": "form_1"}
    assert out["operation"]["serviceId"] == "forms"
    assert out["operation"]["operation"] == "read"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://forms.googleapis.com/v1/forms/form_1"


def test_google_workspace_structured_operation_tool_rejects_wrong_method() -> None:
    out = google_workspace_mcp.drive_read(path="/files", method="POST")

    assert out["ok"] is False
    assert out["error"] == "unsupported_google_workspace_operation_method"
    assert out["allowedMethods"] == ["GET"]


def test_google_workspace_wif_token_flow_signs_delegated_jwt(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("k8s-subject-token")
    calls: list[dict[str, Any]] = []
    credentials = GoogleWorkspaceCredentials(
        auth_mode="workload_identity_federation",
        service_account_email="workspace-agents@project.iam.gserviceaccount.com",
        delegated_subject="admin@bytedesk.test",
        scopes=["https://www.googleapis.com/auth/drive"],
        workload_identity_token_file=str(token_file),
        workload_identity_audience="//iam.googleapis.com/projects/1/pools/p/providers/k8s",
    )

    class Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_post(url: str, **kwargs: Any) -> Response:
        calls.append({"url": url, **kwargs})
        if url == "https://sts.googleapis.com/v1/token":
            return Response({"access_token": "base-token", "expires_in": 3600})
        if url.startswith("https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"):
            return Response({"signedJwt": "signed-workspace-jwt"})
        return Response({"access_token": "workspace-token", "expires_in": 3600})

    monkeypatch.setattr(google_workspace_mcp, "_connection_id", "conn_google")
    monkeypatch.setattr(
        google_workspace_mcp,
        "resolve_google_workspace_credentials",
        lambda connection_id: credentials,
    )
    monkeypatch.setattr(google_workspace_mcp.httpx, "post", fake_post)
    google_workspace_mcp._token_cache.clear()
    google_workspace_mcp._base_token_cache.clear()

    assert google_workspace_mcp._token() == "workspace-token"
    assert calls[0]["data"]["subject_token"] == "k8s-subject-token"
    assert calls[0]["data"]["audience"] == credentials.workload_identity_audience
    assert calls[1]["headers"]["Authorization"] == "Bearer base-token"
    assert "admin@bytedesk.test" in calls[1]["json"]["payload"]
    assert calls[2]["data"]["assertion"] == "signed-workspace-jwt"


def test_google_workspace_wif_can_request_kubernetes_subject_token(
    monkeypatch,
    tmp_path,
) -> None:
    token_file = tmp_path / "token"
    namespace_file = tmp_path / "namespace"
    ca_file = tmp_path / "ca.crt"
    header = {"alg": "none", "typ": "JWT"}
    payload = {"sub": "system:serviceaccount:bytedesk:omnigent-host"}
    encoded_header = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    encoded_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token_file.write_text(f"{encoded_header}.{encoded_payload}.")
    namespace_file.write_text("bytedesk")
    ca_file.write_text("ca")
    calls: list[dict[str, Any]] = []
    credentials = GoogleWorkspaceCredentials(
        auth_mode="workload_identity_federation",
        service_account_email="workspace-agents@project.iam.gserviceaccount.com",
        delegated_subject="admin@bytedesk.test",
        scopes=["https://www.googleapis.com/auth/drive"],
        workload_identity_token_source="kubernetes_token_request",
        workload_identity_audience="//iam.googleapis.com/projects/1/pools/p/providers/k8s",
        kubernetes_token_audience="https://iam.googleapis.com/projects/1/pools/p/providers/k8s",
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"status": {"token": "bounded-k8s-token"}}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            calls.append({"client": kwargs})

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, **kwargs: Any) -> Response:
            calls.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(google_workspace_mcp, "_K8S_TOKEN_PATH", token_file)
    monkeypatch.setattr(google_workspace_mcp, "_K8S_NAMESPACE_PATH", namespace_file)
    monkeypatch.setattr(google_workspace_mcp, "_K8S_CA_PATH", ca_file)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(google_workspace_mcp.httpx, "Client", Client)

    assert google_workspace_mcp._workload_identity_subject_token(credentials) == (
        "bounded-k8s-token"
    )
    assert calls[0]["client"] == {"verify": str(ca_file), "timeout": 20.0}
    assert calls[1]["url"] == (
        "https://kubernetes.default.svc:443/api/v1/namespaces/"
        "bytedesk/serviceaccounts/omnigent-host/token"
    )
    assert calls[1]["json"]["spec"]["audiences"] == [
        "https://iam.googleapis.com/projects/1/pools/p/providers/k8s"
    ]
    assert calls[1]["headers"]["Authorization"] == f"Bearer {token_file.read_text()}"


def test_google_drive_search_calls_drive_files_list(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "url": url, **kwargs})
        return {"ok": True, "data": {"files": [{"id": "file_1", "name": "Roadmap"}]}}

    monkeypatch.setattr(google_workspace_mcp, "_request", fake_request)

    out = google_workspace_mcp.drive_search("name contains 'Roadmap'", page_size=5)

    assert out == {
        "ok": True,
        "files": [{"id": "file_1", "name": "Roadmap"}],
        "nextPageToken": None,
    }
    assert captured["method"] == "GET"
    assert captured["url"] == "https://www.googleapis.com/drive/v3/files"
    assert captured["scopes"] == ["https://www.googleapis.com/auth/drive"]
    assert captured["params"]["q"] == "name contains 'Roadmap'"
    assert captured["params"]["pageSize"] == 5


@dataclass
class _StoredFile:
    id: str = "file_zip_1"
    filename: str = "acme-website.zip"
    bytes: int = 9
    content_type: str | None = "application/zip"
    session_id: str | None = "conv_1"


def test_google_drive_upload_session_file_uses_drive_multipart_upload(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FileStore:
        def get(self, file_id: str, session_id: str | None = None) -> _StoredFile | None:
            assert file_id == "file_zip_1"
            assert session_id == "conv_1"
            return _StoredFile()

    class ArtifactStore:
        def get(self, key: str) -> bytes:
            assert key == "file_zip_1"
            return b"zip-bytes"

    class Response:
        content = b'{"id":"drive_file_1","name":"acme-website.zip"}'
        text = content.decode()
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"id": "drive_file_1", "name": "acme-website.zip"}

    def fake_post(url: str, **kwargs: Any) -> Response:
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("omnigent.runtime.get_file_store", lambda: FileStore())
    monkeypatch.setattr("omnigent.runtime.get_artifact_store", lambda: ArtifactStore())
    monkeypatch.setattr(google_workspace_mcp, "_token", lambda *args, **kwargs: "drive-token")
    monkeypatch.setattr(google_workspace_mcp.httpx, "post", fake_post)

    out = google_workspace_mcp.drive_file_upload_session(
        file_id="file_zip_1",
        session_id="conv_1",
        folder_id="folder_website",
    )

    assert out["ok"] is True
    assert out["file"] == {"id": "drive_file_1", "name": "acme-website.zip"}
    assert out["folder"] == {
        "id": "folder_website",
        "name": "Website",
        "created": False,
    }
    assert out["source"]["file_id"] == "file_zip_1"
    assert captured["url"] == "https://www.googleapis.com/upload/drive/v3/files"
    assert captured["params"]["uploadType"] == "multipart"
    assert captured["headers"]["Authorization"] == "Bearer drive-token"
    assert captured["headers"]["Content-Type"].startswith("multipart/related")
    assert b'"parents":["folder_website"]' in captured["content"]
    assert b"zip-bytes" in captured["content"]


def test_google_drive_upload_session_file_finds_or_creates_website_folder(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FileStore:
        def get(self, file_id: str, session_id: str | None = None) -> _StoredFile | None:
            return _StoredFile()

    class ArtifactStore:
        def get(self, key: str) -> bytes:
            return b"zip-bytes"

    class Response:
        content = b'{"id":"drive_file_1","name":"site.zip"}'
        text = content.decode()
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"id": "drive_file_1", "name": "site.zip"}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            return {"ok": True, "data": {"files": []}}
        return {
            "ok": True,
            "data": {
                "id": "folder_created",
                "name": "Website",
                "mimeType": "application/vnd.google-apps.folder",
            },
        }

    def fake_post(url: str, **kwargs: Any) -> Response:
        calls.append({"method": "POST_UPLOAD", "url": url, **kwargs})
        return Response()

    monkeypatch.setattr("omnigent.runtime.get_file_store", lambda: FileStore())
    monkeypatch.setattr("omnigent.runtime.get_artifact_store", lambda: ArtifactStore())
    monkeypatch.setattr(google_workspace_mcp, "_request", fake_request)
    monkeypatch.setattr(google_workspace_mcp, "_token", lambda *args, **kwargs: "drive-token")
    monkeypatch.setattr(google_workspace_mcp.httpx, "post", fake_post)

    out = google_workspace_mcp.drive_file_upload_session(
        file_id="file_zip_1",
        session_id="conv_1",
        folder_name="Website",
        parent_folder_id="client_folder",
    )

    assert out["ok"] is True
    assert out["folder"] == {"id": "folder_created", "name": "Website", "created": True}
    assert "name = 'Website'" in calls[0]["params"]["q"]
    assert "'client_folder' in parents" in calls[0]["params"]["q"]
    assert calls[1]["json_body"]["name"] == "Website"
    assert calls[1]["json_body"]["parents"] == ["client_folder"]
    assert b'"parents":["folder_created"]' in calls[2]["content"]


def test_google_drive_upload_session_file_rejects_wrong_session(monkeypatch) -> None:
    class FileStore:
        def get(self, file_id: str, session_id: str | None = None) -> None:
            assert file_id == "file_zip_1"
            assert session_id == "conv_other"

    monkeypatch.setattr("omnigent.runtime.get_file_store", lambda: FileStore())
    monkeypatch.setattr("omnigent.runtime.get_artifact_store", lambda: object())

    out = google_workspace_mcp.drive_file_upload_session(
        file_id="file_zip_1",
        session_id="conv_other",
        folder_id="folder_website",
    )

    assert out == {"ok": False, "error": "session_file_not_found"}


def test_google_drive_search_reports_domain_wide_delegation_gap(monkeypatch) -> None:
    def fake_token(*args: Any, **kwargs: Any) -> str:
        response = httpx.Response(
            401,
            json={"error": "unauthorized_client"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        response.raise_for_status()
        raise AssertionError("unreachable")

    monkeypatch.setattr(google_workspace_mcp, "_token", fake_token)

    out = google_workspace_mcp.drive_search("trashed=false", page_size=1)

    assert out == {
        "ok": False,
        "error": "domain_wide_delegation_unauthorized",
        "status": 401,
        "googleError": "unauthorized_client",
        "requiredScopes": ["https://www.googleapis.com/auth/drive"],
    }


def test_google_workspace_token_claims_can_use_requested_scopes() -> None:
    credentials = GoogleWorkspaceCredentials(
        auth_mode="workload_identity_federation",
        service_account_email="workspace-agents@project.iam.gserviceaccount.com",
        delegated_subject="admin@bytedesk.test",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/calendar",
        ],
        workload_identity_audience="//iam.googleapis.com/projects/1/pools/p/providers/k8s",
    )

    claims = google_workspace_mcp._workspace_claims(
        credentials,
        "admin@bytedesk.test",
        100,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    assert claims["scope"] == "https://www.googleapis.com/auth/drive"


def test_google_calendar_meeting_schedule_requests_conference(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "url": url, **kwargs})
        return {"ok": True, "data": {"id": "evt_1"}}

    monkeypatch.setattr(google_workspace_mcp, "_request", fake_request)

    out = google_workspace_mcp.meeting_schedule(
        summary="Demo",
        start={"dateTime": "2026-06-29T10:00:00-04:00"},
        end={"dateTime": "2026-06-29T10:30:00-04:00"},
        attendees=[{"email": "maya@bytedesk.test"}],
    )

    assert out == {"ok": True, "event": {"id": "evt_1"}}
    assert captured["params"]["conferenceDataVersion"] == 1
    assert captured["json_body"]["conferenceData"]["createRequest"]["requestId"].startswith(
        "omnigent-"
    )
    assert captured["json_body"]["attendees"] == [{"email": "maya@bytedesk.test"}]


def test_google_docs_template_merge_copies_then_replaces(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/copy"):
            return {"ok": True, "data": {"id": "doc_1", "name": "Brief"}}
        return {"ok": True, "data": {"replies": []}}

    monkeypatch.setattr(google_workspace_mcp, "_request", fake_request)

    out = google_workspace_mcp.docs_template_merge(
        template_file_id="tmpl_1",
        name="Brief",
        replacements={"{{name}}": "ByteDesk"},
    )

    assert out == {"ok": True, "document": {"id": "doc_1", "name": "Brief"}}
    assert calls[0]["url"] == "https://www.googleapis.com/drive/v3/files/tmpl_1/copy"
    assert calls[1]["url"] == "https://docs.googleapis.com/v1/documents/doc_1:batchUpdate"
    assert calls[1]["json_body"]["requests"][0]["replaceAllText"]["replaceText"] == "ByteDesk"
