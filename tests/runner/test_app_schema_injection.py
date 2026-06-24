"""Tests for ``_inject_mcp_schemas`` — the proxy_stream merge helper.

The runner injects MCP tool schemas into the harness request body
right before forwarding (designs/RUNNER_MCP.md §Schema injection).
The merge must append, not replace, so builtins / client-side tools
from the Omnigent server survive. A future refactor that swaps ``+`` for
``=`` would silently clobber those tools — these tests fail loudly
on that regression.
"""

from __future__ import annotations

from typing import Any

from omnigent.runner.app import _inject_mcp_schemas, _spec_builtin_tool_schemas


def _schema(name: str) -> dict[str, Any]:
    """OpenAI function-tool schema dict for use in expected/actual."""
    return {
        "type": "function",
        "name": name,
        "description": "",
        "parameters": {"type": "object", "properties": {}},
    }


def _nested_schema(name: str) -> dict[str, Any]:
    """Nested OpenAI function-tool schema, as produced by Tool.get_schema()."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_inject_appends_after_existing_tools() -> None:
    """MCP schemas land AFTER the Omnigent server's tools, in order.

    Order matters: the harness's tool list defines a deterministic
    iteration order downstream; flipping it could change which tool
    wins for ambiguous-prompt cases.
    """
    body = {"tools": [_schema("sys_os_read"), _schema("sys_os_write")]}
    mcp = [_schema("jira_search_issues"), _schema("jira_get_issue")]

    _inject_mcp_schemas(body, mcp)

    assert [t["name"] for t in body["tools"]] == [
        "sys_os_read",
        "sys_os_write",
        "jira_search_issues",
        "jira_get_issue",
    ]


def test_inject_creates_tools_when_missing() -> None:
    """A body with no ``tools`` key gets one populated from the MCP list."""
    body: dict[str, Any] = {}
    mcp = [_schema("jira_search_issues")]

    _inject_mcp_schemas(body, mcp)

    assert body["tools"] == mcp


def test_inject_treats_none_tools_as_empty() -> None:
    """``tools: None`` (vs missing) is also handled — the ``or []`` guard."""
    body: dict[str, Any] = {"tools": None}
    mcp = [_schema("jira_search_issues")]

    _inject_mcp_schemas(body, mcp)

    assert body["tools"] == mcp


def test_inject_noop_on_empty_mcp_schemas() -> None:
    """Empty MCP list leaves the body's tools list untouched (not even rewritten)."""
    body = {"tools": [_schema("sys_os_read")]}
    original_tools_id = id(body["tools"])

    _inject_mcp_schemas(body, [])

    assert body["tools"] == [_schema("sys_os_read")]
    # Same list object: nothing copied/reassigned. Guards against a
    # future refactor that always replaces the list, dropping any
    # downstream references.
    assert id(body["tools"]) == original_tools_id


def test_inject_does_not_share_list_with_caller() -> None:
    """The result list is independent of the mcp_schemas argument.

    A later mutation to the mcp_schemas list (e.g. eviction-driven
    cache rebuild) must not retroactively edit the in-flight request.
    """
    body: dict[str, Any] = {"tools": [_schema("a")]}
    mcp = [_schema("jira_search_issues")]

    _inject_mcp_schemas(body, mcp)
    # Mutate the source after injection.
    mcp.append(_schema("jira_get_issue"))

    assert [t["name"] for t in body["tools"]] == ["a", "jira_search_issues"], (
        "in-flight request must not see post-injection mutation of mcp_schemas"
    )


def test_inject_skips_mcp_already_present() -> None:
    """An MCP schema already in ``body["tools"]`` is not appended again.

    Regression: the per-session tool cache already includes MCP
    schemas, so re-injecting them sent duplicate tool names that codex rejects.
    """
    body = {"tools": [_schema("sys_os_read"), _schema("confluence_get_service_info")]}
    mcp = [_schema("confluence_get_service_info"), _schema("confluence_search_pages")]

    _inject_mcp_schemas(body, mcp)

    names = [t["name"] for t in body["tools"]]
    assert names == ["sys_os_read", "confluence_get_service_info", "confluence_search_pages"]
    assert names.count("confluence_get_service_info") == 1


def test_inject_skips_nested_schema_already_present_after_normalize() -> None:
    """Nested builtins de-dupe before the executor flattens dynamic tools.

    Regression: the streaming path injects nested builtin schemas such as
    ``load_skill``. If the injector only checks top-level ``name`` fields, the
    executor later flattens both copies and Codex rejects the turn with
    ``duplicate dynamic tool name: load_skill``.
    """
    from omnigent.runtime.harnesses._executor_adapter import _normalize_tool_schemas

    body = {"tools": [_nested_schema("load_skill"), _nested_schema("sys_session_list")]}
    mcp = [_nested_schema("load_skill"), _nested_schema("sys_agent_list")]

    _inject_mcp_schemas(body, mcp)

    names = [t.get("name") for t in _normalize_tool_schemas(body["tools"])]
    assert names == ["load_skill", "sys_session_list", "sys_agent_list"]
    assert names.count("load_skill") == 1


# ---------------------------------------------------------------------------
# _spec_builtin_tool_schemas — the streaming path's builtin assembly (BDP-2204)
# ---------------------------------------------------------------------------


def _builtin_names(schemas: list[dict[str, Any]]) -> set[str]:
    """Builtin schemas are nested OpenAI shape — name is under ``function``."""
    out: set[str] = set()
    for s in schemas:
        fn = s.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            out.add(fn["name"])
    return out


def test_builtin_schemas_include_spawn_orchestration_tools() -> None:
    """A spawn-enabled spec yields the sys_* orchestration builtins.

    These are exactly the tools Maya's prompt drives (sys_agent_list /
    sys_session_create / sys_session_send / sys_read_inbox). The streaming
    turn path must inject them or the model gets "No such tool available:
    mcp__omnigent__sys_agent_list" (BDP-2204). Mirrors test_manager's
    spawn-gate assertion at the app-helper boundary.
    """
    from omnigent.spec.types import AgentSpec

    names = _builtin_names(
        _spec_builtin_tool_schemas(AgentSpec(spec_version=1, spawn=True), None)
    )
    assert "sys_agent_list" in names
    assert "sys_session_create" in names
    assert "sys_session_send" in names
    assert "sys_read_inbox" in names


def test_builtin_schemas_empty_for_none_spec() -> None:
    """No spec → no builtins (degrades to MCP-only, never raises)."""
    assert _spec_builtin_tool_schemas(None, None) == []


def test_builtin_schemas_inject_then_normalize_round_trip() -> None:
    """Injected nested builtins survive the inject + flatten to a callable name.

    Locks in the end-to-end shape contract: nested builtin schemas append
    into ``body["tools"]`` and ``_normalize_tool_schemas`` exposes a
    top-level ``name`` (→ ``mcp__omnigent__sys_agent_list``), so the empty-name
    failure mode cannot silently return.
    """
    from omnigent.runtime.harnesses._executor_adapter import _normalize_tool_schemas
    from omnigent.spec.types import AgentSpec

    body: dict[str, Any] = {"tools": []}
    _inject_mcp_schemas(
        body, _spec_builtin_tool_schemas(AgentSpec(spec_version=1, spawn=True), None)
    )
    flat_names = {s.get("name") for s in _normalize_tool_schemas(body["tools"])}
    assert "sys_agent_list" in flat_names
