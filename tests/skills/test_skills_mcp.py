"""Unit tests for the skills MCP stdio front (BDP-2462).

The front is a thin FastMCP server whose tools proxy one authenticated HTTP
call each to the existing ``/v1/skills/*`` (and ``/v1/agents``) server routes.
Tests exercise two seams:

* tool → request mapping (each tool builds the right method/path/body and
  unwraps the response), by monkeypatching ``_request``;
* the auth/transport layer (login once, cache the bearer, re-login on a single
  401), by monkeypatching the low-level ``_raw`` transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from bytedesk_omnigent import skills_mcp


@pytest.fixture(autouse=True)
def _reset_token() -> None:
    skills_mcp._reset_token_cache()
    yield
    skills_mcp._reset_token_cache()


class _Recorder:
    """Captures (method, path, body) and returns a canned payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None = None) -> Any:
        self.calls.append((method, path, body))
        return self.payload


# --- tool → request mapping -------------------------------------------------


def test_search_posts_query_and_unwraps_data(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"object": "skill_search.result", "data": [{"name": "x"}], "errors": []})
    monkeypatch.setattr(skills_mcp, "_request", rec)

    out = skills_mcp.search("seo", sources=["skills"], limit=3)

    assert rec.calls == [
        ("POST", "/v1/skills/search", {"query": "seo", "sources": ["skills"], "limit": 3})
    ]
    assert out["results"] == [{"name": "x"}]
    assert out["errors"] == []


def test_sources_gets_and_unwraps(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"object": "skill_source.list", "data": [{"id": "skills"}]})
    monkeypatch.setattr(skills_mcp, "_request", rec)

    out = skills_mcp.sources()

    assert rec.calls == [("GET", "/v1/skills/sources", None)]
    assert out["sources"] == [{"id": "skills"}]


def test_stage_preview_posts_install_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder({"id": "skprev_1", "operation": "install", "skills": [], "target_actions": []})
    monkeypatch.setattr(skills_mcp, "_request", rec)

    out = skills_mcp.stage_preview(
        source="skills",
        source_ref="coreyhaines31/marketingskills@seo-audit",
        target_agent_ids=["a1", "a2"],
        install_mode="skip_existing",
    )

    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v1/skills/previews")
    assert body["operation"] == "install"
    assert body["source"] == "skills"
    assert body["source_ref"] == "coreyhaines31/marketingskills@seo-audit"
    assert body["target_agent_ids"] == ["a1", "a2"]
    assert body["install_mode"] == "skip_existing"
    assert out["preview_id"] == "skprev_1"


def test_apply_preview_posts_to_apply_route(monkeypatch: pytest.MonkeyPatch) -> None:
    applied = [{"agent_id": "a1", "status": "applied"}]
    rec = _Recorder({"object": "skill_apply.result", "data": applied})
    monkeypatch.setattr(skills_mcp, "_request", rec)

    out = skills_mcp.apply_preview("skprev_1", agent_ids=["a1"])

    assert rec.calls == [
        ("POST", "/v1/skills/previews/skprev_1/apply", {"target_agent_ids": ["a1"]})
    ]
    assert out["results"] == applied


def test_remove_stages_remove_preview_then_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove() is the rollback primitive: preview(operation=remove) → apply."""
    preview = {"id": "skprev_rm", "operation": "remove", "skills": [], "target_actions": []}
    apply_res = {"object": "skill_apply.result", "data": [{"agent_id": "a1", "status": "applied"}]}

    seq: list[Any] = [preview, apply_res]
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, body: dict | None = None) -> Any:
        calls.append((method, path, body))
        return seq.pop(0)

    monkeypatch.setattr(skills_mcp, "_request", fake_request)

    out = skills_mcp.remove("seo-audit", target_agent_ids=["a1"])

    assert calls[0][0:2] == ("POST", "/v1/skills/previews")
    assert calls[0][2]["operation"] == "remove"
    assert calls[0][2]["skill_names"] == ["seo-audit"]
    assert calls[1] == (
        "POST",
        "/v1/skills/previews/skprev_rm/apply",
        {"target_agent_ids": ["a1"]},
    )
    assert out["results"] == [{"agent_id": "a1", "status": "applied"}]


# --- resolve_targets scope mapping -----------------------------------------

_AGENTS = {
    "object": "list",
    "data": [
        {"id": "a1", "display_name": "Priya", "department": "Engineering", "workflow": False},
        {"id": "a2", "display_name": "Elias", "department": "Engineering", "workflow": False},
        {"id": "a3", "display_name": "Nova", "department": "Marketing", "workflow": False},
        {"id": "wf", "display_name": "Router", "department": "Engineering", "workflow": True},
    ],
}


def test_resolve_targets_organization_excludes_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills_mcp, "_request", _Recorder(_AGENTS))
    out = skills_mcp.resolve_targets("organization")
    assert sorted(t["id"] for t in out["targets"]) == ["a1", "a2", "a3"]


def test_resolve_targets_department_filters_by_department(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills_mcp, "_request", _Recorder(_AGENTS))
    out = skills_mcp.resolve_targets("department:Engineering")
    assert sorted(t["id"] for t in out["targets"]) == ["a1", "a2"]


def test_resolve_targets_employee_by_id_or_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills_mcp, "_request", _Recorder(_AGENTS))
    assert [t["id"] for t in skills_mcp.resolve_targets("employee:a3")["targets"]] == ["a3"]
    assert [t["id"] for t in skills_mcp.resolve_targets("Priya")["targets"]] == ["a1"]


# --- auth/transport layer ---------------------------------------------------


def test_request_is_anonymous_first_no_login_when_route_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Read routes are open to anonymous service callers, so a headerless 200
    # must NOT trigger a login (the runner has no creds anyway).
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "pw")
    raw_calls: list[tuple[str, str, str | None]] = []

    def fake_raw(method: str, url: str, headers: dict, json: Any) -> tuple[int, Any]:
        raw_calls.append((method, url, headers.get("Authorization")))
        return 200, {"ok": True}

    monkeypatch.setattr(skills_mcp, "_raw", fake_raw)

    assert skills_mcp._request("GET", "/v1/skills/sources") == {"ok": True}
    assert not any(c[1].endswith("/auth/login") for c in raw_calls)  # no login
    assert raw_calls[0][2] is None  # first call is headerless


def test_request_authenticates_on_401_when_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_HOST_AUTH_PASSWORD", "pw")
    seen_bearers: list[str | None] = []

    def fake_raw(method: str, url: str, headers: dict, json: Any) -> tuple[int, Any]:
        if url.endswith("/auth/login"):
            return 200, {"token": "TKN"}
        seen_bearers.append(headers.get("Authorization"))
        # Anonymous (no bearer) 401s; after login the bearer call succeeds.
        return (401, {"detail": "unauthorized"}) if headers.get("Authorization") is None else (
            200,
            {"ok": True},
        )

    monkeypatch.setattr(skills_mcp, "_raw", fake_raw)

    assert skills_mcp._request("GET", "/v1/skills/installed") == {"ok": True}
    assert seen_bearers == [None, "Bearer TKN"]  # headerless first, then bearer


def test_request_raises_clear_error_on_401_without_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runner case: a user-gated route 401s and there are no host creds to
    # fall back on — surface a clear, actionable error (not a crash).
    monkeypatch.delenv("OMNIGENT_HOST_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("OMNIGENT_HOST_AUTH_PASSWORD", raising=False)

    def fake_raw(method: str, url: str, headers: dict, json: Any) -> tuple[int, Any]:
        return 401, {"detail": "unauthorized"}

    monkeypatch.setattr(skills_mcp, "_raw", fake_raw)

    with pytest.raises(RuntimeError, match="runner holds no server credential"):
        skills_mcp._request("POST", "/v1/skills/previews/x/apply", {})
