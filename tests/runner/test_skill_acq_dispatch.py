"""Tests for the sys_skill_* skill-acquisition tool dispatch (BDP-2487).

Each tool proxies a ``/v1/skills/*`` route (or ``/v1/agents`` for scope
resolution) over the runner's ``server_client`` — which carries the runner
tunnel token, so the ``require_user`` mutating routes resolve the session owner.
These pin the request shapes (method/path/body) and the response unwrapping so a
refactor can't silently misroute an install.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runner.tool_dispatch import _execute_skill_acq_tool

# ── Fake httpx client ───────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx response stub keyed off a canned JSON body."""

    def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


class _FakeClient:
    """Records get/post calls and replays a queue of canned responses."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params, None))
        return self._responses.pop(0)

    async def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, None, json))
        return self._responses.pop(0)


# ── search / sources / installed ────────────────────────────────


@pytest.mark.asyncio
async def test_search_posts_and_unwraps() -> None:
    client = _FakeClient([_FakeResponse({"data": [{"name": "o/r@s"}], "errors": ["x"]})])
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_search",
            {"query": "pdf", "sources": ["skills"], "limit": 5},
            client,  # type: ignore[arg-type]
        )
    )
    method, url, _params, body = client.calls[0]
    assert (method, url) == ("POST", "/v1/skills/search")
    assert body == {"query": "pdf", "limit": 5, "sources": ["skills"]}
    assert out == {"results": [{"name": "o/r@s"}], "errors": ["x"]}


@pytest.mark.asyncio
async def test_sources_gets_and_unwraps() -> None:
    client = _FakeClient([_FakeResponse({"data": [{"id": "skills", "usable": True}]})])
    out = json.loads(await _execute_skill_acq_tool("sys_skill_sources", {}, client))  # type: ignore[arg-type]
    assert client.calls[0][:2] == ("GET", "/v1/skills/sources")
    assert out == {"sources": [{"id": "skills", "usable": True}]}


@pytest.mark.asyncio
async def test_installed_scopes_by_agent_id() -> None:
    client = _FakeClient([_FakeResponse({"data": [{"name": "geo"}]})])
    out = json.loads(
        await _execute_skill_acq_tool("sys_skill_installed", {"agent_id": "ag1"}, client)  # type: ignore[arg-type]
    )
    method, url, params, _ = client.calls[0]
    assert (method, url) == ("GET", "/v1/skills/installed")
    assert params == {"agent_id": "ag1"}
    assert out == {"installed": [{"name": "geo"}]}


@pytest.mark.asyncio
async def test_installed_without_agent_id_omits_param() -> None:
    client = _FakeClient([_FakeResponse({"data": []})])
    await _execute_skill_acq_tool("sys_skill_installed", {}, client)  # type: ignore[arg-type]
    assert client.calls[0][2] is None


# ── resolve_targets scope filtering ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_targets_filters_by_scope() -> None:
    agents = {
        "data": [
            {"id": "a1", "display_name": "Alice", "department": "Operations"},
            {"id": "a2", "display_name": "Bob", "department": "Sales"},
            {"id": "w1", "display_name": "Router", "department": "Operations", "workflow": True},
        ]
    }
    client = _FakeClient([_FakeResponse(agents)])
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_resolve_targets", {"scope": "department:operations"}, client  # type: ignore[arg-type]
        )
    )
    method, url, params, _ = client.calls[0]
    assert (method, url) == ("GET", "/v1/agents")
    assert params == {"limit": 1000, "order": "asc"}
    # Alice in Operations; Router excluded (workflow); Bob in Sales.
    assert out == {"targets": [{"id": "a1", "display_name": "Alice", "department": "Operations"}]}


@pytest.mark.asyncio
async def test_resolve_targets_matches_employee_by_name_slug() -> None:
    # A built-in's id is a generated ag_… hash and its display_name may be
    # capitalized/absent, so an employee scope must match the stable `name`
    # slug too (regression: it only matched id + display_name → empty targets).
    agents = {
        "data": [
            {
                "id": "ag_07b3",
                "name": "structured-output-demo",
                "display_name": "Structured Output Demo",
            },
            {"id": "ag_x", "name": "other", "display_name": "Other"},
        ]
    }
    client = _FakeClient([_FakeResponse(agents)])
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_resolve_targets",
            {"scope": "employee:structured-output-demo"},
            client,  # type: ignore[arg-type]
        )
    )
    assert [t["id"] for t in out["targets"]] == ["ag_07b3"]


@pytest.mark.asyncio
async def test_resolve_targets_organization_excludes_workflow() -> None:
    agents = {
        "data": [
            {"id": "a1", "display_name": "Alice", "department": "Ops"},
            {"id": "w1", "display_name": "Router", "department": "Ops", "workflow": True},
        ]
    }
    client = _FakeClient([_FakeResponse(agents)])
    raw = await _execute_skill_acq_tool(
        "sys_skill_resolve_targets", {"scope": "organization"}, client  # type: ignore[arg-type]
    )
    out = json.loads(raw)
    assert [t["id"] for t in out["targets"]] == ["a1"]


@pytest.mark.asyncio
async def test_resolve_targets_requires_scope() -> None:
    client = _FakeClient([])
    out = json.loads(
        await _execute_skill_acq_tool("sys_skill_resolve_targets", {}, client)  # type: ignore[arg-type]
    )
    assert "error" in out
    assert client.calls == []


# ── stage_preview / apply ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_preview_install_body_and_unwrap() -> None:
    client = _FakeClient(
        [_FakeResponse({"id": "pv1", "skills": [{"name": "geo"}], "target_actions": [{"a": 1}]})]
    )
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_stage_preview",
            {"source": "skills", "source_ref": "o/r@s", "target_agent_ids": ["a1"]},
            client,  # type: ignore[arg-type]
        )
    )
    method, url, _, body = client.calls[0]
    assert (method, url) == ("POST", "/v1/skills/previews")
    assert body == {
        "operation": "install",
        "target_agent_ids": ["a1"],
        "install_mode": "skip_existing",
        "source": "skills",
        "source_ref": "o/r@s",
    }
    assert out == {"preview_id": "pv1", "skills": [{"name": "geo"}], "target_actions": [{"a": 1}]}


@pytest.mark.asyncio
async def test_apply_posts_to_preview_apply() -> None:
    client = _FakeClient([_FakeResponse({"data": [{"agent_id": "a1", "status": "ok"}]})])
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_apply", {"preview_id": "pv1", "agent_ids": ["a1"]}, client  # type: ignore[arg-type]
        )
    )
    method, url, _, body = client.calls[0]
    assert (method, url) == ("POST", "/v1/skills/previews/pv1/apply")
    assert body == {"target_agent_ids": ["a1"]}
    assert out == {"results": [{"agent_id": "a1", "status": "ok"}]}


@pytest.mark.asyncio
async def test_apply_requires_preview_id() -> None:
    client = _FakeClient([])
    out = json.loads(await _execute_skill_acq_tool("sys_skill_apply", {}, client))  # type: ignore[arg-type]
    assert "error" in out
    assert client.calls == []


# ── remove = preview-then-apply ─────────────────────────────────


@pytest.mark.asyncio
async def test_remove_stages_remove_preview_then_applies() -> None:
    client = _FakeClient(
        [
            _FakeResponse({"id": "pv-rm"}),
            _FakeResponse({"data": [{"agent_id": "a1", "status": "removed"}]}),
        ]
    )
    out = json.loads(
        await _execute_skill_acq_tool(
            "sys_skill_remove", {"skill_name": "geo", "target_agent_ids": ["a1"]}, client  # type: ignore[arg-type]
        )
    )
    # First call stages a remove preview.
    m0, u0, _, b0 = client.calls[0]
    assert (m0, u0) == ("POST", "/v1/skills/previews")
    assert b0 == {"operation": "remove", "target_agent_ids": ["a1"], "skill_names": ["geo"]}
    # Second call applies that preview to the same targets.
    m1, u1, _, b1 = client.calls[1]
    assert (m1, u1) == ("POST", "/v1/skills/previews/pv-rm/apply")
    assert b1 == {"target_agent_ids": ["a1"]}
    assert out == {"results": [{"agent_id": "a1", "status": "removed"}]}


# ── error surfacing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_server_client_errors() -> None:
    out = json.loads(await _execute_skill_acq_tool("sys_skill_sources", {}, None))
    assert "error" in out and "server access" in out["error"]


@pytest.mark.asyncio
async def test_server_error_status_is_surfaced() -> None:
    client = _FakeClient([_FakeResponse({"detail": "nope"}, status_code=401)])
    raw = await _execute_skill_acq_tool("sys_skill_apply", {"preview_id": "pv1"}, client)  # type: ignore[arg-type]
    out = json.loads(raw)
    assert "error" in out
    assert "401" in out["error"]
    assert "nope" in out["error"]
