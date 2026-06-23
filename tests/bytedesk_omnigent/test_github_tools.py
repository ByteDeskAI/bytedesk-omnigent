"""Tests for the native ``bytedesk_github`` agent tool (BDP-2404).

The HTTP layer is mocked with ``httpx.MockTransport`` — no real network. The
adapter takes credentials directly (bypassing the secret backend) except in the
dedicated "not configured" tests, which monkeypatch ``load_secret``.

Mirrors ``test_confluence_tools.py`` (BDP-2403) — same shape, same error
contract. Read + comment only: no merge/push/create-PR write operations exist.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from bytedesk_omnigent.tools.github_tools import (
    BytedeskGitHubTool,
    _GitHubClient,
)
from omnigent.tools.base import ToolContext

_BASE = "https://api.github.com"
_TOKEN = "ghp_test-token"
_REPO = "acme/widget"

_CTX = ToolContext(task_id="t1", agent_id="ag_1")


def _make_tool(
    handler, *, default_repo: str | None = _REPO
) -> tuple[BytedeskGitHubTool, list[httpx.Request]]:
    """Build a tool whose adapter routes every request through ``handler``.

    Returns the tool and a list that captures each issued request for assertions.
    """
    captured: list[httpx.Request] = []

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_capturing)
    client = httpx.Client(base_url=_BASE, transport=transport)
    adapter = _GitHubClient(
        base_url=_BASE, token=_TOKEN, default_repo=default_repo, client=client
    )
    return BytedeskGitHubTool(client=adapter), captured


def _call(tool: BytedeskGitHubTool, **args: Any) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(args), _CTX))


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ── auth / headers ──────────────────────────────────────────────────────────────


def test_requests_send_bearer_token_and_github_headers():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"id": 1}))
    _call(tool, op="get_repo")

    req = captured[0]
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_token_value_is_not_echoed_in_results():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={"id": 1}))
    raw = tool.invoke(json.dumps({"op": "get_repo"}), _CTX)
    assert _TOKEN not in raw


# ── get_repo ─────────────────────────────────────────────────────────────────────


def test_get_repo_uses_default_repo():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"id": 1, "full_name": _REPO})
    )
    result = _call(tool, op="get_repo")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}"
    assert result["ok"] is True
    assert result["repo"]["full_name"] == _REPO


def test_get_repo_honors_repo_override():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={"id": 2}))
    _call(tool, op="get_repo", repo="other/proj")

    assert captured[0].url.path == "/repos/other/proj"


# ── list_prs ─────────────────────────────────────────────────────────────────────


def test_list_prs_passes_state_and_per_page():
    body = [{"number": 1, "title": "PR one"}]
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="list_prs", state="closed", per_page=5)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/pulls"
    assert req.url.params["state"] == "closed"
    assert req.url.params["per_page"] == "5"
    assert result["ok"] is True
    assert result["pull_requests"] == body


def test_list_prs_defaults_state_open():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=[]))
    _call(tool, op="list_prs")
    assert captured[0].url.params["state"] == "open"


# ── get_pr ───────────────────────────────────────────────────────────────────────


def test_get_pr_returns_summary_fields():
    body = {
        "number": 42,
        "title": "Add feature",
        "state": "open",
        "merged": False,
        "mergeable": True,
        "head": {"sha": "abc123"},
        "base": {"ref": "develop"},
        "html_url": "https://github.com/acme/widget/pull/42",
    }
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_pr", number=42)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/pulls/42"
    assert result["ok"] is True
    pr = result["pr"]
    assert pr["number"] == 42
    assert pr["title"] == "Add feature"
    assert pr["state"] == "open"
    assert pr["merged"] is False
    assert pr["mergeable"] is True
    assert pr["head_sha"] == "abc123"
    assert pr["base_ref"] == "develop"
    assert pr["html_url"] == "https://github.com/acme/widget/pull/42"


def test_get_pr_requires_number():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="get_pr")
    assert result["ok"] is False
    assert "number" in result["error"]
    assert captured == []


# ── get_pr_files ─────────────────────────────────────────────────────────────────


def test_get_pr_files_returns_filtered_fields():
    body = [
        {
            "filename": "a.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "patch": "@@ -1 +1 @@",
            "sha": "ignored",
        }
    ]
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_pr_files", number=7, per_page=50)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/pulls/7/files"
    assert req.url.params["per_page"] == "50"
    assert result["ok"] is True
    files = result["files"]
    assert files == [
        {
            "filename": "a.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "patch": "@@ -1 +1 @@",
        }
    ]


def test_get_pr_files_defaults_per_page_100():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=[]))
    _call(tool, op="get_pr_files", number=7)
    assert captured[0].url.params["per_page"] == "100"


# ── get_pr_checks (GET-then-GET, aggregation) ───────────────────────────────────


def _pr_then_checks(sha: str, runs: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/13"):
            return httpx.Response(200, json={"number": 13, "head": {"sha": sha}})
        if request.url.path == f"/repos/{_REPO}/commits/{sha}/check-runs":
            return httpx.Response(200, json={"check_runs": runs})
        return httpx.Response(404, json={})

    return handler


def test_get_pr_checks_all_success_overall_success():
    runs = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "test", "status": "completed", "conclusion": "success"},
    ]
    tool, captured = _make_tool(_pr_then_checks("sha-ok", runs))

    result = _call(tool, op="get_pr_checks", number=13)

    # two GETs: resolve head.sha, then check-runs for that sha
    assert captured[0].method == "GET"
    assert captured[0].url.path == f"/repos/{_REPO}/pulls/13"
    assert captured[1].method == "GET"
    assert captured[1].url.path == f"/repos/{_REPO}/commits/sha-ok/check-runs"

    checks = result["checks"]
    assert result["ok"] is True
    assert checks["sha"] == "sha-ok"
    assert checks["total"] == 2
    assert checks["by_conclusion"]["success"] == 2
    assert checks["overall"] == "success"
    assert {r["name"] for r in checks["runs"]} == {"build", "test"}


def test_get_pr_checks_any_failure_overall_failure():
    runs = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "test", "status": "completed", "conclusion": "failure"},
    ]
    tool, _ = _make_tool(_pr_then_checks("sha-bad", runs))

    result = _call(tool, op="get_pr_checks", number=13)

    checks = result["checks"]
    assert checks["by_conclusion"]["success"] == 1
    assert checks["by_conclusion"]["failure"] == 1
    assert checks["overall"] == "failure"


def test_get_pr_checks_incomplete_overall_pending():
    runs = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "deploy", "status": "in_progress", "conclusion": None},
    ]
    tool, _ = _make_tool(_pr_then_checks("sha-run", runs))

    result = _call(tool, op="get_pr_checks", number=13)

    checks = result["checks"]
    assert checks["overall"] == "pending"


def test_get_pr_checks_empty_overall_pending():
    tool, _ = _make_tool(_pr_then_checks("sha-none", []))
    result = _call(tool, op="get_pr_checks", number=13)
    checks = result["checks"]
    assert checks["total"] == 0
    assert checks["overall"] == "pending"


def test_get_pr_checks_requires_number():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="get_pr_checks")
    assert result["ok"] is False
    assert captured == []


# ── get_issue ────────────────────────────────────────────────────────────────────


def test_get_issue_path_and_passthrough():
    body = {"number": 9, "title": "Bug", "state": "open"}
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_issue", number=9)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/issues/9"
    assert result["ok"] is True
    assert result["issue"] == body


def test_get_issue_requires_number():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    assert _call(tool, op="get_issue")["ok"] is False
    assert captured == []


# ── list_issues ──────────────────────────────────────────────────────────────────


def test_list_issues_passes_state_labels_per_page():
    body = [{"number": 1, "title": "i"}]
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(
        tool, op="list_issues", state="all", labels="bug,p0", per_page=10
    )

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/issues"
    assert req.url.params["state"] == "all"
    assert req.url.params["labels"] == "bug,p0"
    assert req.url.params["per_page"] == "10"
    assert result["ok"] is True
    assert result["issues"] == body


def test_list_issues_defaults_state_open_and_omits_labels():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=[]))
    _call(tool, op="list_issues")
    assert captured[0].url.params["state"] == "open"
    assert "labels" not in captured[0].url.params


# ── search_issues ────────────────────────────────────────────────────────────────


def test_search_issues_passes_query_and_returns_total_and_items():
    body = {
        "total_count": 2,
        "items": [
            {
                "number": 5,
                "title": "Issue A",
                "state": "open",
                "html_url": "https://github.com/acme/widget/issues/5",
                "body": "ignored",
            },
            {
                "number": 6,
                "title": "Issue B",
                "state": "closed",
                "html_url": "https://github.com/acme/widget/issues/6",
            },
        ],
    }
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="search_issues", query="repo:acme/widget is:open")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/search/issues"
    assert req.url.params["q"] == "repo:acme/widget is:open"
    assert result["ok"] is True
    assert result["total_count"] == 2
    assert result["items"] == [
        {
            "number": 5,
            "title": "Issue A",
            "state": "open",
            "html_url": "https://github.com/acme/widget/issues/5",
        },
        {
            "number": 6,
            "title": "Issue B",
            "state": "closed",
            "html_url": "https://github.com/acme/widget/issues/6",
        },
    ]


def test_search_issues_requires_query():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="search_issues")
    assert result["ok"] is False
    assert "query" in result["error"]
    assert captured == []


def test_search_issues_does_not_need_repo():
    # search is global, so the missing-repo guard must not fire even with no repo.
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"total_count": 0, "items": []}),
        default_repo=None,
    )
    result = _call(tool, op="search_issues", query="is:open")
    assert result["ok"] is True
    assert captured[0].url.path == "/search/issues"


# ── list_commits ─────────────────────────────────────────────────────────────────


def test_list_commits_passes_sha_and_per_page():
    body = [{"sha": "c1"}]
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="list_commits", sha="develop", per_page=15)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/commits"
    assert req.url.params["sha"] == "develop"
    assert req.url.params["per_page"] == "15"
    assert result["ok"] is True
    assert result["commits"] == body


def test_list_commits_omits_sha_when_absent():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=[]))
    _call(tool, op="list_commits")
    assert "sha" not in captured[0].url.params
    assert captured[0].url.params["per_page"] == "30"


# ── get_file (base64 decode + non-crash fallback) ───────────────────────────────


def test_get_file_decodes_base64_content():
    body = {
        "name": "README.md",
        "path": "README.md",
        "encoding": "base64",
        "content": _b64("hello file"),
    }
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_file", path="README.md", ref="main")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"/repos/{_REPO}/contents/README.md"
    assert req.url.params["ref"] == "main"
    assert result["ok"] is True
    assert result["file"]["path"] == "README.md"
    assert result["file"]["content"] == "hello file"


def test_get_file_passes_through_non_base64_encoding():
    body = {"name": "x", "path": "x", "encoding": "none", "content": ""}
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=body))
    result = _call(tool, op="get_file", path="x")
    assert result["ok"] is True
    # not base64 → no decoded text, raw metadata returned without crashing
    assert result["file"]["encoding"] == "none"
    assert "content" not in result["file"] or result["file"]["content"] == ""


def test_get_file_binary_or_undecodable_does_not_crash():
    # content that is not valid UTF-8 once decoded → fallback to raw metadata.
    raw_b64 = base64.b64encode(b"\xff\xfe\x00\x01").decode()
    body = {
        "name": "logo.png",
        "path": "logo.png",
        "encoding": "base64",
        "content": raw_b64,
        "size": 4,
    }
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_file", path="logo.png")

    assert result["ok"] is True
    # decode failed → no decoded text, metadata preserved, no exception
    file = result["file"]
    assert file["path"] == "logo.png"
    assert file.get("decoded") is not True


def test_get_file_requires_path():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="get_file")
    assert result["ok"] is False
    assert "path" in result["error"]
    assert captured == []


def test_get_file_omits_ref_when_absent():
    body = {"name": "a", "path": "a", "encoding": "base64", "content": _b64("x")}
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))
    _call(tool, op="get_file", path="a")
    assert "ref" not in captured[0].url.params


# ── add_comment ──────────────────────────────────────────────────────────────────


def test_add_comment_posts_issue_comment_body():
    tool, captured = _make_tool(
        lambda r: httpx.Response(201, json={"id": 555})
    )

    result = _call(tool, op="add_comment", number=21, body="LGTM")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == f"/repos/{_REPO}/issues/21/comments"
    sent = json.loads(req.content)
    assert sent == {"body": "LGTM"}
    assert result["ok"] is True
    assert result["comment"]["id"] == 555


def test_add_comment_requires_number_and_body():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    assert _call(tool, op="add_comment", number=21)["ok"] is False
    assert _call(tool, op="add_comment", body="x")["ok"] is False
    assert captured == []


# ── repo configuration ───────────────────────────────────────────────────────────


def test_missing_repo_returns_github_repo_not_configured():
    # no repo arg AND no default repo → structured error, no network
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={}), default_repo=None
    )
    result = _call(tool, op="get_repo")
    assert result == {"ok": False, "error": "github_repo_not_configured"}
    assert captured == []


def test_repo_arg_works_without_default_repo():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json={"id": 1}), default_repo=None
    )
    result = _call(tool, op="get_repo", repo="x/y")
    assert result["ok"] is True
    assert captured[0].url.path == "/repos/x/y"


# ── graceful errors ──────────────────────────────────────────────────────────────


def test_unknown_op_returns_structured_error():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json={}))
    result = _call(tool, op="frobnicate")
    assert result["ok"] is False
    assert "unknown op" in result["error"]
    assert captured == []


def test_http_404_returns_structured_error_not_raised():
    tool, _ = _make_tool(
        lambda r: httpx.Response(404, json={"message": "Not Found"})
    )
    result = _call(tool, op="get_repo")
    assert result["ok"] is False
    assert result["error"] == "github_http_error"
    assert result["status"] == 404


def test_http_500_returns_structured_error_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(500, text="boom"))
    result = _call(tool, op="list_prs")
    assert result["ok"] is False
    assert result["error"] == "github_http_error"
    assert result["status"] == 500


def test_transport_error_returns_request_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    tool, _ = _make_tool(handler)
    result = _call(tool, op="get_repo")
    assert result["ok"] is False
    assert result["error"] == "github_request_failed"


def test_missing_credentials_returns_github_not_configured(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: None)

    tool = BytedeskGitHubTool()  # default adapter → secret backend
    result = _call(tool, op="get_repo", repo="x/y")
    assert result == {"ok": False, "error": "github_not_configured"}


def test_invalid_arguments_json_is_graceful():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json={}))
    result = json.loads(tool.invoke("{not json", _CTX))
    assert result["ok"] is False
    assert result["error"] == "invalid_arguments_json"


# ── credential resolution + fallbacks ───────────────────────────────────────────


def test_token_falls_back_to_bytedesk_github_token(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {"BYTEDESK_GITHUB_TOKEN": "fallback-tok", "GITHUB_REPO": "a/b"}
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _GitHubClient()
    adapter._resolve_credentials()
    assert adapter._token == "fallback-tok"
    assert adapter._default_repo == "a/b"


def test_token_prefers_github_token_over_fallback(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {
        "GITHUB_TOKEN": "primary-tok",
        "BYTEDESK_GITHUB_TOKEN": "fallback-tok",
    }
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _GitHubClient()
    adapter._resolve_credentials()
    assert adapter._token == "primary-tok"


# ── tool surface ─────────────────────────────────────────────────────────────────


def test_tool_name_and_schema():
    assert BytedeskGitHubTool.name() == "bytedesk_github"
    schema = BytedeskGitHubTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_github"
    assert schema["function"]["parameters"]["required"] == ["op"]
    ops = schema["function"]["parameters"]["properties"]["op"]["enum"]
    assert set(ops) == {
        "get_repo",
        "list_prs",
        "get_pr",
        "get_pr_files",
        "get_pr_checks",
        "get_issue",
        "list_issues",
        "search_issues",
        "list_commits",
        "get_file",
        "add_comment",
    }


def test_tool_is_registered_in_extension_factories():
    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_github" in factories
    tool = factories["bytedesk_github"](object())
    assert tool.name() == "bytedesk_github"


_OPS = [
    "get_repo",
    "list_prs",
    "get_pr",
    "get_pr_files",
    "get_pr_checks",
    "get_issue",
    "list_issues",
    "search_issues",
    "list_commits",
    "get_file",
    "add_comment",
]


@pytest.mark.parametrize("op", _OPS)
def test_every_op_is_dispatchable(op):
    # Each declared op reaches a handler (here: a missing-arg structured error for
    # the ones that require args, or a 200 for the no-required-arg ones), proving
    # the dispatcher routes every enum value rather than falling through to
    # "unknown op".
    tool, _ = _make_tool(
        lambda r: httpx.Response(200, json={"check_runs": [], "items": []})
    )
    result = _call(tool, op=op)
    # whatever the outcome, it must NOT be the unknown-op fallthrough
    if result["ok"] is False:
        assert "unknown op" not in result["error"]
