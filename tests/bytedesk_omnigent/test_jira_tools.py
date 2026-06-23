"""Tests for the native ``bytedesk_jira`` agent tool (BDP-2402).

The HTTP layer is mocked with ``httpx.MockTransport`` — no real network. The
adapter takes credentials directly (bypassing the secret backend) except in the
dedicated "not configured" test, which monkeypatches ``load_secret``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from bytedesk_omnigent.tools.jira_tools import (
    BytedeskJiraTool,
    _adf_doc,
    _JiraClient,
)
from omnigent.tools.base import ToolContext

_BASE = "https://acme.atlassian.net"
_EMAIL = "agent@acme.test"
_TOKEN = "s3cr3t-token"

_CTX = ToolContext(task_id="t1", agent_id="ag_1")


def _expected_basic() -> str:
    raw = base64.b64encode(f"{_EMAIL}:{_TOKEN}".encode()).decode()
    return f"Basic {raw}"


def _make_tool(handler) -> tuple[BytedeskJiraTool, list[httpx.Request]]:
    """Build a tool whose adapter routes every request through ``handler``.

    Returns the tool and a list that captures each issued request for assertions.
    """
    captured: list[httpx.Request] = []

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_capturing)
    client = httpx.Client(base_url=_BASE, transport=transport)
    adapter = _JiraClient(
        base_url=_BASE, email=_EMAIL, api_token=_TOKEN, client=client
    )
    return BytedeskJiraTool(client=adapter), captured


def _call(tool: BytedeskJiraTool, **args: Any) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(args), _CTX))


# ── ADF wrapping ───────────────────────────────────────────────────────────────


def test_adf_doc_wraps_plain_text():
    doc = _adf_doc("hello world")
    assert doc == {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello world"}],
            }
        ],
    }


def test_adf_doc_blank_is_still_valid_document():
    doc = _adf_doc("")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert doc["content"] == [{"type": "paragraph", "content": []}]


# ── auth ────────────────────────────────────────────────────────────────────────


def test_search_sends_basic_auth_header():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"issues": []})
    )
    _call(tool, op="search", jql="project = BDP")

    assert captured[0].headers["Authorization"] == _expected_basic()
    assert captured[0].headers["Accept"] == "application/json"


# ── search ──────────────────────────────────────────────────────────────────────


def test_search_posts_jql_and_normalizes_results():
    body = {
        "issues": [
            {
                "key": "BDP-1",
                "fields": {
                    "summary": "Do the thing",
                    "status": {"name": "In Progress"},
                    "assignee": {"displayName": "Ryan"},
                },
            }
        ]
    }
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="search", jql="project = BDP", max_results=5)

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/rest/api/3/search"
    sent = json.loads(req.content)
    assert sent["jql"] == "project = BDP"
    assert sent["maxResults"] == 5
    assert result["ok"] is True
    assert result["issues"] == [
        {
            "key": "BDP-1",
            "summary": "Do the thing",
            "status": "In Progress",
            "assignee": "Ryan",
        }
    ]


def test_search_requires_jql():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"issues": []}))
    result = _call(tool, op="search")
    assert result["ok"] is False
    assert "jql" in result["error"]
    assert captured == []  # never hit the network


def test_search_clamps_max_results():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"issues": []}))
    _call(tool, op="search", jql="x", max_results=9999)
    assert json.loads(captured[0].content)["maxResults"] == 100


# ── get_issue ───────────────────────────────────────────────────────────────────


def test_get_issue_gets_path():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"key": "BDP-7", "fields": {}})
    )
    result = _call(tool, op="get_issue", key="BDP-7")

    assert captured[0].method == "GET"
    assert captured[0].url.path == "/rest/api/3/issue/BDP-7"
    assert result["ok"] is True
    assert result["issue"]["key"] == "BDP-7"


def test_get_issue_requires_key():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="get_issue")
    assert result["ok"] is False
    assert captured == []


# ── add_comment ─────────────────────────────────────────────────────────────────


def test_add_comment_posts_adf_body():
    tool, captured = _make_tool(lambda r: httpx.Response(201, json={"id": "10001"}))

    result = _call(tool, op="add_comment", key="BDP-3", body="looks good")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/rest/api/3/issue/BDP-3/comment"
    sent = json.loads(req.content)
    assert sent["body"] == _adf_doc("looks good")
    assert result["ok"] is True
    assert result["comment"]["id"] == "10001"


def test_add_comment_requires_key_and_body():
    tool, captured = _make_tool(lambda r: httpx.Response(201, json={}))
    assert _call(tool, op="add_comment", key="BDP-3")["ok"] is False
    assert _call(tool, op="add_comment", body="x")["ok"] is False
    assert captured == []


# ── transition ──────────────────────────────────────────────────────────────────


def test_transition_resolves_name_then_posts_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "name": "To Do"},
                        {"id": "21", "name": "In Progress"},
                    ]
                },
            )
        return httpx.Response(204)

    tool, captured = _make_tool(handler)
    result = _call(tool, op="transition", key="BDP-9", transition_name_or_id="in progress")

    # GET transitions, then POST the resolved id.
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/rest/api/3/issue/BDP-9/transitions"
    assert captured[1].method == "POST"
    assert captured[1].url.path == "/rest/api/3/issue/BDP-9/transitions"
    assert json.loads(captured[1].content) == {"transition": {"id": "21"}}
    assert result["ok"] is True
    assert result["transition"] == "In Progress"
    assert result["transition_id"] == "21"


def test_transition_resolves_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "21", "name": "Done"}]})
        return httpx.Response(204)

    tool, captured = _make_tool(handler)
    result = _call(tool, op="transition", key="BDP-9", transition_name_or_id="21")
    assert result["ok"] is True
    assert json.loads(captured[1].content) == {"transition": {"id": "21"}}


def test_transition_unknown_returns_structured_error_without_post():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"transitions": [{"id": "11", "name": "To Do"}]})
    )
    result = _call(tool, op="transition", key="BDP-9", transition_name_or_id="Nope")
    assert result["ok"] is False
    assert result["error"] == "transition_not_found"
    assert result["available"] == ["To Do"]
    # Only the GET fired; no POST against a missing transition.
    assert len(captured) == 1


# ── create_issue ────────────────────────────────────────────────────────────────


def test_create_issue_posts_fields_with_adf_description():
    tool, captured = _make_tool(
        lambda r: httpx.Response(201, json={"key": "BDP-42", "id": "10042"})
    )

    result = _call(
        tool,
        op="create_issue",
        project_key="BDP",
        summary="New work",
        description="details here",
    )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/rest/api/3/issue"
    fields = json.loads(req.content)["fields"]
    assert fields["project"] == {"key": "BDP"}
    assert fields["summary"] == "New work"
    assert fields["issuetype"] == {"name": "Task"}  # defaults to Task
    assert fields["description"] == _adf_doc("details here")
    assert "parent" not in fields
    assert result["ok"] is True
    assert result["created"]["key"] == "BDP-42"


def test_create_issue_includes_parent_and_custom_type():
    tool, captured = _make_tool(lambda r: httpx.Response(201, json={"key": "BDP-43"}))

    _call(
        tool,
        op="create_issue",
        project_key="BDP",
        summary="child",
        issue_type="Subtask",
        parent="BDP-1",
    )

    fields = json.loads(captured[0].content)["fields"]
    assert fields["issuetype"] == {"name": "Subtask"}
    assert fields["parent"] == {"key": "BDP-1"}


def test_create_issue_requires_project_and_summary():
    tool, captured = _make_tool(lambda r: httpx.Response(201, json={}))
    assert _call(tool, op="create_issue", project_key="BDP")["ok"] is False
    assert _call(tool, op="create_issue", summary="x")["ok"] is False
    assert captured == []


# ── graceful errors ─────────────────────────────────────────────────────────────


def test_unknown_op_returns_structured_error():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="frobnicate")
    assert result["ok"] is False
    assert "unknown op" in result["error"]
    assert captured == []


def test_http_4xx_returns_structured_error_not_raised():
    tool, _ = _make_tool(
        lambda r: httpx.Response(400, json={"errorMessages": ["bad jql"]})
    )
    result = _call(tool, op="search", jql="busted")
    assert result["ok"] is False
    assert result["error"] == "jira_http_error"
    assert result["status"] == 400


def test_http_5xx_returns_structured_error_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(503, text="down"))
    result = _call(tool, op="get_issue", key="BDP-1")
    assert result["ok"] is False
    assert result["error"] == "jira_http_error"
    assert result["status"] == 503


def test_missing_credentials_returns_jira_not_configured(monkeypatch):
    # Adapter with NO injected creds resolves through the secret backend; stub it
    # to return empty so the tool reports a graceful structured error.
    import omnigent.onboarding.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: None)

    tool = BytedeskJiraTool()  # default adapter → secret backend
    result = _call(tool, op="search", jql="project = BDP")
    assert result == {"ok": False, "error": "jira_not_configured"}


def test_invalid_arguments_json_is_graceful():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={}))
    result = json.loads(tool.invoke("{not json", _CTX))
    assert result["ok"] is False
    assert result["error"] == "invalid_arguments_json"


# ── tool surface ────────────────────────────────────────────────────────────────


def test_tool_name_and_schema():
    assert BytedeskJiraTool.name() == "bytedesk_jira"
    schema = BytedeskJiraTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_jira"
    assert schema["function"]["parameters"]["required"] == ["op"]
    ops = schema["function"]["parameters"]["properties"]["op"]["enum"]
    assert set(ops) == {
        "search",
        "get_issue",
        "add_comment",
        "transition",
        "create_issue",
    }


def test_tool_is_registered_in_extension_factories():
    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_jira" in factories
    tool = factories["bytedesk_jira"](object())
    assert tool.name() == "bytedesk_jira"


_OPS = ["search", "get_issue", "add_comment", "transition", "create_issue"]


@pytest.mark.parametrize("op", _OPS)
def test_every_op_is_dispatchable(op):
    # Each declared op reaches a handler (here: a missing-arg structured error),
    # proving the dispatcher routes every enum value rather than falling through
    # to "unknown op".
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={"issues": [], "transitions": []}))
    result = _call(tool, op=op)
    assert result["ok"] is False
    assert "unknown op" not in result["error"]
