"""Tests for the native ``bytedesk_confluence`` agent tool (BDP-2403).

The HTTP layer is mocked with ``httpx.MockTransport`` — no real network. The
adapter takes credentials directly (bypassing the secret backend) except in the
dedicated "not configured" test, which monkeypatches ``load_secret``.

Mirrors ``test_jira_tools.py`` (BDP-2402) — same shape, same error contract.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from bytedesk_omnigent.connectors.store import ConnectorConnection
from bytedesk_omnigent.tools.confluence_tools import (
    BytedeskConfluenceTool,
    _ConfluenceClient,
    _storage_body,
)
from omnigent.tools.base import ToolContext

_BASE = "https://acme.atlassian.net"
_EMAIL = "agent@acme.test"
_TOKEN = "s3cr3t-token"

_CTX = ToolContext(task_id="t1", agent_id="ag_1")


def _expected_basic() -> str:
    raw = base64.b64encode(f"{_EMAIL}:{_TOKEN}".encode()).decode()
    return f"Basic {raw}"


def _make_tool(handler) -> tuple[BytedeskConfluenceTool, list[httpx.Request]]:
    """Build a tool whose adapter routes every request through ``handler``.

    Returns the tool and a list that captures each issued request for assertions.
    """
    captured: list[httpx.Request] = []

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_capturing)
    client = httpx.Client(base_url=_BASE, transport=transport)
    adapter = _ConfluenceClient(base_url=_BASE, email=_EMAIL, api_token=_TOKEN, client=client)
    return BytedeskConfluenceTool(client=adapter), captured


def _call(tool: BytedeskConfluenceTool, **args: Any) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(args), _CTX))


# ── storage-body wrapping ────────────────────────────────────────────────────────


def test_storage_body_wraps_plain_text():
    assert _storage_body("hello world") == "<p>hello world</p>"


def test_storage_body_leaves_markup_alone():
    markup = "<p>already <strong>marked up</strong></p>"
    assert _storage_body(markup) == markup


def test_storage_body_blank_is_empty_paragraph():
    assert _storage_body("") == "<p></p>"


# ── auth ──────────────────────────────────────────────────────────────────────────


def test_search_sends_basic_auth_header():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    _call(tool, op="search", cql="type=page")

    assert captured[0].headers["Authorization"] == _expected_basic()
    assert captured[0].headers["Accept"] == "application/json"


def test_connection_backed_search_uses_atlassian_cloud_prefix(monkeypatch):
    conn = ConnectorConnection(
        id="conn_1",
        provider="atlassian",
        display_name="Acme",
        auth_type="oauth_3lo",
        status="connected",
        scopes=[],
        metadata={"cloud_id": "cloud-1"},
        secret_ref="secret-ref",
        last_health_status=None,
        last_health_at=None,
        last_error=None,
        created_at=1,
        updated_at=1,
        version=1,
    )

    class _Store:
        def get_connection(self, connection_id: str):
            return conn

    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.credentials.get_connector_store",
        lambda: _Store(),
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.credentials.load_connector_secret",
        lambda ref: {"access_token": "oauth-token", "cloud_id": "cloud-1"},
    )
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(
        base_url="https://api.atlassian.com",
        transport=httpx.MockTransport(_handler),
    )
    tool = BytedeskConfluenceTool(
        client=_ConfluenceClient(connection_id="conn_1", client=client)
    )

    result = _call(tool, op="search", cql="type=page")

    assert result["ok"] is True
    assert captured[0].headers["Authorization"] == "Bearer oauth-token"
    assert captured[0].url.path == "/ex/confluence/cloud-1/wiki/rest/api/content/search"


def test_connection_backed_search_can_use_connector_secret_references(monkeypatch):
    conn = ConnectorConnection(
        id="conn_1",
        provider="atlassian",
        display_name="Acme",
        auth_type="oauth_3lo",
        status="connected",
        scopes=[],
        metadata={
            "auth_mode": "api_token",
            "base_url_secret": "ATLASSIAN_BASE_URL",
            "email_secret": "ATLASSIAN_EMAIL",
            "api_token_secret": "ATLASSIAN_API_TOKEN",
        },
        secret_ref=None,
        last_health_status=None,
        last_health_at=None,
        last_error=None,
        created_at=1,
        updated_at=1,
        version=1,
    )

    class _Store:
        def get_connection(self, connection_id: str):
            return conn

    secrets = {
        "ATLASSIAN_BASE_URL": f"{_BASE}/wiki",
        "ATLASSIAN_EMAIL": _EMAIL,
        "ATLASSIAN_API_TOKEN": _TOKEN,
    }
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.credentials.get_connector_store",
        lambda: _Store(),
    )
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: secrets.get(name),
    )
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(base_url=_BASE, transport=httpx.MockTransport(_handler))
    tool = BytedeskConfluenceTool(
        client=_ConfluenceClient(connection_id="conn_1", client=client)
    )

    result = _call(tool, op="search", cql="type=page")

    assert result["ok"] is True
    assert captured[0].headers["Authorization"] == _expected_basic()
    assert captured[0].url.path == "/wiki/rest/api/content/search"


# ── search ──────────────────────────────────────────────────────────────────────


def test_search_passes_cql_and_limit():
    body = {"results": [{"id": "1", "title": "Page A"}]}
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="search", cql="type=page AND space=BDP", limit=5)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/wiki/rest/api/content/search"
    assert req.url.params["cql"] == "type=page AND space=BDP"
    assert req.url.params["limit"] == "5"
    assert result["ok"] is True
    assert result["results"] == [{"id": "1", "title": "Page A"}]


def test_search_requires_cql():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    result = _call(tool, op="search")
    assert result["ok"] is False
    assert "cql" in result["error"]
    assert captured == []  # never hit the network


def test_search_defaults_limit():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    _call(tool, op="search", cql="type=page")
    assert captured[0].url.params["limit"] == "20"


def test_search_clamps_limit():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    _call(tool, op="search", cql="type=page", limit=9999)
    assert captured[0].url.params["limit"] == "250"


def test_search_invalid_limit_falls_back_to_default():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    _call(tool, op="search", cql="type=page", limit="not-a-number")
    assert captured[0].url.params["limit"] == "20"


# ── get_page ─────────────────────────────────────────────────────────────────────


def test_get_page_v2_path_and_returns_fields():
    body = {
        "id": "12345",
        "title": "My Page",
        "version": {"number": 7},
        "body": {"storage": {"value": "<p>hi</p>"}},
    }
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_page", page_id="12345")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/wiki/api/v2/pages/12345"
    assert req.url.params["body-format"] == "storage"
    assert result["ok"] is True
    page = result["page"]
    assert page["id"] == "12345"
    assert page["title"] == "My Page"
    assert page["version"] == 7
    assert page["body"] == "<p>hi</p>"


def test_get_page_requires_page_id():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="get_page")
    assert result["ok"] is False
    assert captured == []


# ── create_page ──────────────────────────────────────────────────────────────────


def test_create_page_posts_v2_with_storage_body():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"id": "999", "title": "New"}))

    result = _call(
        tool,
        op="create_page",
        space_id="SP1",
        title="New",
        body="hello body",
    )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/wiki/api/v2/pages"
    sent = json.loads(req.content)
    assert sent["spaceId"] == "SP1"
    assert sent["status"] == "current"
    assert sent["title"] == "New"
    assert sent["body"] == {
        "representation": "storage",
        "value": "<p>hello body</p>",
    }
    assert "parentId" not in sent
    assert result["ok"] is True
    assert result["page"]["id"] == "999"


def test_create_page_includes_parent_id():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"id": "1000"}))

    _call(
        tool,
        op="create_page",
        space_id="SP1",
        title="Child",
        body="<p>x</p>",
        parent_id="42",
    )

    sent = json.loads(captured[0].content)
    assert sent["parentId"] == "42"
    # already-markup body is not re-wrapped
    assert sent["body"]["value"] == "<p>x</p>"


def test_create_page_resolves_space_key_to_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [{"id": "SPACE-99", "key": "BDP"}]})
        return httpx.Response(200, json={"id": "777"})

    tool, captured = _make_tool(handler)
    result = _call(tool, op="create_page", space_key="BDP", title="T", body="b")

    # first GET resolves the key, then POST creates with the resolved id
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/wiki/api/v2/spaces"
    assert captured[0].url.params["keys"] == "BDP"
    assert captured[1].method == "POST"
    assert json.loads(captured[1].content)["spaceId"] == "SPACE-99"
    assert result["ok"] is True


def test_create_page_requires_space_and_title():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    assert _call(tool, op="create_page", title="x", body="b")["ok"] is False
    assert _call(tool, op="create_page", space_id="SP1", body="b")["ok"] is False
    assert captured == []


def test_create_page_unresolvable_space_key_errors_without_post():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    result = _call(tool, op="create_page", space_key="NOPE", title="T", body="b")
    assert result["ok"] is False
    assert result["error"] == "space_key_not_found"
    assert len(captured) == 1  # only the resolve GET fired, no POST


# ── update_page ──────────────────────────────────────────────────────────────────


def test_update_page_with_explicit_version_increments_and_puts():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"id": "55", "version": {"number": 4}})
    )

    result = _call(
        tool,
        op="update_page",
        page_id="55",
        title="Updated",
        body="new content",
        version=3,
    )

    req = captured[0]
    assert req.method == "PUT"
    assert req.url.path == "/wiki/api/v2/pages/55"
    sent = json.loads(req.content)
    assert sent["id"] == "55"
    assert sent["status"] == "current"
    assert sent["title"] == "Updated"
    assert sent["body"] == {
        "representation": "storage",
        "value": "<p>new content</p>",
    }
    assert sent["version"] == {"number": 4}  # 3 + 1
    assert result["ok"] is True


def test_update_page_without_version_gets_then_puts():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "55", "title": "Old", "version": {"number": 9}},
            )
        return httpx.Response(200, json={"id": "55", "version": {"number": 10}})

    tool, captured = _make_tool(handler)
    result = _call(tool, op="update_page", page_id="55", title="T", body="b")

    # GET current version, then PUT version+1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/wiki/api/v2/pages/55"
    assert captured[1].method == "PUT"
    assert json.loads(captured[1].content)["version"] == {"number": 10}  # 9 + 1
    assert result["ok"] is True


def test_update_page_requires_page_id_and_title():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    assert _call(tool, op="update_page", title="x", body="b")["ok"] is False
    assert _call(tool, op="update_page", page_id="55", body="b")["ok"] is False
    assert captured == []


def test_update_page_rejects_invalid_version():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="update_page", page_id="55", title="T", version="nope")
    assert result["ok"] is False
    assert result["error"] == "invalid 'version'"
    assert captured == []


# ── add_comment ──────────────────────────────────────────────────────────────────


def test_add_comment_posts_footer_comment_with_storage_body():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"id": "c1"}))

    result = _call(tool, op="add_comment", page_id="55", body="looks good")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/wiki/api/v2/footer-comments"
    sent = json.loads(req.content)
    assert sent["pageId"] == "55"
    assert sent["body"] == {
        "representation": "storage",
        "value": "<p>looks good</p>",
    }
    assert result["ok"] is True
    assert result["comment"]["id"] == "c1"


def test_add_comment_requires_page_id_and_body():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    assert _call(tool, op="add_comment", page_id="55")["ok"] is False
    assert _call(tool, op="add_comment", body="x")["ok"] is False
    assert captured == []


# ── graceful errors ──────────────────────────────────────────────────────────────


def test_unknown_op_returns_structured_error():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="frobnicate")
    assert result["ok"] is False
    assert "unknown op" in result["error"]
    assert captured == []


def test_http_4xx_returns_structured_error_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(404, json={"message": "no such page"}))
    result = _call(tool, op="get_page", page_id="missing")
    assert result["ok"] is False
    assert result["error"] == "confluence_http_error"
    assert result["status"] == 404


def test_http_5xx_returns_structured_error_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(500, text="boom"))
    result = _call(tool, op="search", cql="type=page")
    assert result["ok"] is False
    assert result["error"] == "confluence_http_error"
    assert result["status"] == 500


def test_transport_error_returns_request_failed(monkeypatch):
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    tool, _ = _make_tool(_boom)
    result = _call(tool, op="search", cql="type=page")
    assert result == {"ok": False, "error": "confluence_request_failed"}


def test_http_lazy_client_is_created_without_injected_client(monkeypatch):
    created: list[str] = []
    real_client = httpx.Client

    def _fake_client(**kwargs):
        created.append(kwargs.get("base_url", ""))
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        return real_client(transport=transport)

    monkeypatch.setattr(httpx, "Client", _fake_client)
    adapter = _ConfluenceClient(base_url=_BASE, email=_EMAIL, api_token=_TOKEN)
    first = adapter._http()
    second = adapter._http()
    assert first is second
    assert created == [_BASE]


def test_missing_credentials_returns_confluence_not_configured(monkeypatch):
    # Adapter with NO injected creds resolves through the secret backend; stub it
    # to return empty so the tool reports a graceful structured error.
    import omnigent.onboarding.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: None)

    tool = BytedeskConfluenceTool()  # default adapter → secret backend
    result = _call(tool, op="search", cql="type=page")
    assert result == {"ok": False, "error": "confluence_not_configured"}


def test_invalid_arguments_json_is_graceful():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={}))
    result = json.loads(tool.invoke("{not json", _CTX))
    assert result["ok"] is False
    assert result["error"] == "invalid_arguments_json"


# ── credential fallbacks (share the Atlassian account) ───────────────────────────


def test_credentials_fall_back_to_jira_secrets(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {
        "JIRA_BASE_URL": "https://acme.atlassian.net",
        "JIRA_EMAIL": _EMAIL,
        "JIRA_API_TOKEN": _TOKEN,
    }
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _ConfluenceClient()
    adapter._resolve_credentials()
    assert adapter._base_url == "https://acme.atlassian.net"
    assert adapter._email == _EMAIL
    assert adapter._api_token == _TOKEN


def test_base_url_strips_trailing_wiki(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {
        "CONFLUENCE_BASE_URL": "https://acme.atlassian.net/wiki/",
        "ATLASSIAN_EMAIL": _EMAIL,
        "ATLASSIAN_API_TOKEN": _TOKEN,
    }
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _ConfluenceClient()
    adapter._resolve_credentials()
    assert adapter._base_url == "https://acme.atlassian.net"


# ── tool surface ─────────────────────────────────────────────────────────────────


def test_tool_name_and_schema():
    assert BytedeskConfluenceTool.name() == "bytedesk_confluence"
    schema = BytedeskConfluenceTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_confluence"
    assert schema["function"]["parameters"]["required"] == ["op"]
    ops = schema["function"]["parameters"]["properties"]["op"]["enum"]
    assert set(ops) == {
        "search",
        "get_page",
        "create_page",
        "update_page",
        "add_comment",
    }


def test_tool_is_registered_in_extension_factories():
    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_confluence" in factories
    tool = factories["bytedesk_confluence"](object())
    assert tool.name() == "bytedesk_confluence"


_OPS = ["search", "get_page", "create_page", "update_page", "add_comment"]


@pytest.mark.parametrize("op", _OPS)
def test_every_op_is_dispatchable(op):
    # Each declared op reaches a handler (here: a missing-arg structured error),
    # proving the dispatcher routes every enum value rather than falling through
    # to "unknown op".
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={"results": []}))
    result = _call(tool, op=op)
    assert result["ok"] is False
    assert "unknown op" not in result["error"]
