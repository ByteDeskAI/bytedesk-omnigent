"""Edge-path coverage for :mod:`omnigent.runner.mcp_manager` helpers."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import ElicitResult, Tool as McpToolDef

from omnigent.runner.mcp_manager import (
    McpSchemasResult,
    RunnerMcpManager,
    _POOL_SPEC_CAPACITY,
    _SpecEntry,
    _build_accept_content,
    _mcp_tool_schema,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig
pytestmark = pytest.mark.asyncio

from tests.runner.test_mcp_manager import (
    _make_config,
    _make_spec,
    _make_tool_def,
    patch_connection,
)

def test_build_accept_content_returns_none_without_schema() -> None:
    params = SimpleNamespace(message="approve?")
    assert _build_accept_content(params) is None


def test_build_accept_content_fills_from_requested_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.tools._elicitation_schema.build_accept_content_from_schema",
        lambda schema: {"field": schema["default"]},
    )
    params = SimpleNamespace(
        message="approve?",
        requestedSchema={"type": "object", "default": "yes"},
    )
    assert _build_accept_content(params) == {"field": "yes"}


def test_mcp_tool_schema_filters_disallowed_tools() -> None:
    tool = _make_tool_def("hidden")
    assert _mcp_tool_schema("github", tool, allowed={"other"}) is None


@pytest.mark.asyncio
async def test_prewarm_noop_when_spec_has_no_mcp_servers() -> None:
    manager = RunnerMcpManager()
    await manager.prewarm(AgentSpec(spec_version=1, name="bare"))
    assert manager._specs == {}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_schemas_for_surfaces_partial_results_when_prewarm_task_raises(
    patch_connection: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_connection["__tools_for__"]["good"] = [_make_tool_def("ok_tool")]

    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("good"))

    async def _boom(_entry: _SpecEntry) -> None:
        raise RuntimeError("connect exploded")

    monkeypatch.setattr(manager, "_connect_all", _boom)
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.runner.mcp_manager"):
            result = await manager.schemas_for(spec)
    finally:
        await manager.shutdown()

    assert isinstance(result.schemas, list)


@pytest.mark.asyncio
async def test_call_tool_raises_when_spec_has_no_mcp_servers() -> None:
    manager = RunnerMcpManager()
    spec = AgentSpec(spec_version=1, name="no-mcp")
    with pytest.raises(RuntimeError, match="no MCPs registered"):
        await manager.call_tool(spec, "github__search", {})
    await manager.shutdown()


@pytest.mark.asyncio
async def test_call_tool_bootstraps_pool_via_schemas_for(
    patch_connection: dict[str, Any],
) -> None:
    patch_connection["__tools_for__"]["github"] = [_make_tool_def("search")]
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    try:
        output = await manager.call_tool(spec, "github__search", {"q": "asyncio"})
    finally:
        await manager.shutdown()
    assert output == "called search with {'q': 'asyncio'}"
    assert patch_connection["github"].connect_calls == 1


@pytest.mark.asyncio
async def test_call_tool_raises_when_pool_entry_stays_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))

    async def _noop_schemas_for(_spec: AgentSpec) -> McpSchemasResult:
        return McpSchemasResult(schemas=[], tool_names=set(), failures={})

    monkeypatch.setattr(manager, "schemas_for", _noop_schemas_for)
    with pytest.raises(RuntimeError, match="failed to initialize MCPs"):
        await manager.call_tool(spec, "github__search", {})
    await manager.shutdown()


@pytest.mark.asyncio
async def test_call_tool_accepts_bare_tool_name(patch_connection: dict[str, Any]) -> None:
    patch_connection["__tools_for__"]["github"] = [_make_tool_def("search")]
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    try:
        await manager.schemas_for(spec)
        output = await manager.call_tool(spec, "search", {"q": "x"})
    finally:
        await manager.shutdown()
    assert output == "called search with {'q': 'x'}"


def test_resolve_owning_server_returns_none_without_mcp_servers() -> None:
    manager = RunnerMcpManager()
    spec = AgentSpec(spec_version=1, name="no-mcp")
    assert manager._resolve_owning_server(spec, "search") is None


@pytest.mark.asyncio
async def test_resolve_owning_server_returns_none_without_pool_entry() -> None:
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    assert manager._resolve_owning_server(spec, "search") is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_resolve_owning_server_skips_failed_servers_and_returns_none_for_missing_tool(
    patch_connection: dict[str, Any],
) -> None:
    patch_connection["__tools_for__"]["good"] = [_make_tool_def("search")]
    patch_connection["__raise_for__"]["bad"] = RuntimeError("upstream down")
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("bad"), _make_config("good"))
    try:
        await manager.schemas_for(spec)
        assert manager._resolve_owning_server(spec, "search") is not None
        assert manager._resolve_owning_server(spec, "missing_tool") is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_resolve_owning_server_finds_connected_tool(
    patch_connection: dict[str, Any],
) -> None:
    patch_connection["__tools_for__"]["github"] = [_make_tool_def("search")]
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    try:
        await manager.schemas_for(spec)
        server = manager._resolve_owning_server(spec, "search")
    finally:
        await manager.shutdown()
    assert server is not None
    assert server.config.name == "github"


@pytest.mark.asyncio
async def test_shutdown_logs_close_errors(
    patch_connection: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_connection["__tools_for__"]["github"] = [_make_tool_def("search")]

    class _CloseBoom:
        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            self._config = config
            self._inner = patch_connection.setdefault(
                config.name,
                type("FC", (), {"connect_calls": 0, "close_calls": 0})(),
            )

        async def connect(self) -> list[McpToolDef]:
            return [_make_tool_def("search")]

        async def close(self) -> None:
            raise RuntimeError("close failed")

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            return "ok"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("omnigent.runner.mcp_manager.McpServerConnection", _CloseBoom)
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    try:
        await manager.schemas_for(spec)
        with caplog.at_level(logging.ERROR, logger="omnigent.runner.mcp_manager"):
            await manager.shutdown()
    finally:
        monkeypatch.undo()

    assert any("error closing MCP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_evict_skips_missing_entry_and_cancels_inflight_prewarm() -> None:
    manager = RunnerMcpManager()
    ghost_hash = "ghost-hash"
    victim_hash = "victim-hash"
    manager._lru = [ghost_hash, victim_hash] + [
        f"spec-{i}" for i in range(_POOL_SPEC_CAPACITY)
    ]
    manager._specs = {
        f"spec-{i}": _SpecEntry(spec_hash=f"spec-{i}") for i in range(_POOL_SPEC_CAPACITY)
    }
    victim_entry = _SpecEntry(spec_hash=victim_hash)
    task = asyncio.create_task(asyncio.sleep(3600))
    victim_entry.prewarm_task = task
    manager._specs[victim_hash] = victim_entry

    manager._evict_if_needed()
    assert ghost_hash not in manager._lru
    assert victim_hash not in manager._specs
    await asyncio.sleep(0)
    assert task.cancelled() or task.cancelling() or task.done()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_safe_close_logs_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    conn = AsyncMock()
    conn.close.side_effect = RuntimeError("close boom")
    with caplog.at_level(logging.ERROR, logger="omnigent.runner.mcp_manager"):
        await RunnerMcpManager._safe_close(conn, "spec-x", "github")
    assert any("error closing evicted MCP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_connect_all_skips_already_connected_servers(
    patch_connection: dict[str, Any],
) -> None:
    patch_connection["__tools_for__"]["github"] = [_make_tool_def("search")]
    manager = RunnerMcpManager()
    spec = _make_spec(_make_config("github"))
    try:
        await manager.schemas_for(spec)
        entry = next(iter(manager._specs.values()))
        connect_calls_before = patch_connection["github"].connect_calls
        await manager._connect_all(entry)
        assert patch_connection["github"].connect_calls == connect_calls_before
    finally:
        await manager.shutdown()


def test_status_snapshot_skips_missing_lru_entries() -> None:
    manager = RunnerMcpManager()
    manager._lru = ["ghost", "real"]
    manager._specs["real"] = _SpecEntry(spec_hash="real")
    snapshot = manager.status_snapshot()
    assert [s["spec_hash"] for s in snapshot["specs"]] == ["real"]


@pytest.mark.asyncio
async def test_elicitation_callback_declines_without_server_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RunnerMcpManager(server_client=None)
    callback = manager._build_elicitation_callback()
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.mcp_manager"):
        result = await callback("conv_1", SimpleNamespace(message="approve?"))
    assert result.action == "decline"


@pytest.mark.asyncio
async def test_elicitation_callback_declines_when_post_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = AsyncMock()
    client.post.side_effect = RuntimeError("network down")
    manager = RunnerMcpManager(server_client=client)
    callback = manager._build_elicitation_callback()
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.mcp_manager"):
        result = await callback("conv_1", SimpleNamespace(message="approve?"))
    assert result.action == "decline"


@pytest.mark.asyncio
async def test_elicitation_callback_declines_when_server_returns_no_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {}
    client = AsyncMock()
    client.post.return_value = response
    manager = RunnerMcpManager(server_client=client)
    callback = manager._build_elicitation_callback()
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.mcp_manager"):
        result = await callback("conv_1", SimpleNamespace(message="approve?"))
    assert result.action == "decline"


@pytest.mark.asyncio
async def test_elicitation_callback_accepts_with_schema_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"elicitation_id": "elicit_1"}
    client = AsyncMock()
    client.post.return_value = response

    async def _approve(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        "omnigent.runner.pending_approvals.wait_for_user_approval",
        _approve,
    )
    monkeypatch.setattr(
        "omnigent.runner.mcp_manager._build_accept_content",
        lambda _params: {"answer": "yes"},
    )

    manager = RunnerMcpManager(server_client=client)
    callback = manager._build_elicitation_callback()
    params = SimpleNamespace(
        message="approve?",
        requestedSchema={"type": "object"},
    )
    result = await callback("conv_1", params)
    assert result.action == "accept"
    assert result.content == {"answer": "yes"}


@pytest.mark.asyncio
async def test_elicitation_callback_declines_when_user_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"elicitation_id": "elicit_2"}
    client = AsyncMock()
    client.post.return_value = response

    async def _deny(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "omnigent.runner.pending_approvals.wait_for_user_approval",
        _deny,
    )
    manager = RunnerMcpManager(server_client=client)
    callback = manager._build_elicitation_callback()
    result = await callback("conv_1", SimpleNamespace(message="approve?"))
    assert result.action == "decline"