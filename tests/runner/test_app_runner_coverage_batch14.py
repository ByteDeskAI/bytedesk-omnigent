"""Batch-14 coverage for the last runner.app gaps."""

from __future__ import annotations

import uuid

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_agent_ids_ref
from omnigent.runner.resource_registry import OMNIGENT_REPL_TERMINAL_ROLE, SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_mcp_summarize_edges import _FakeMcpManager
from tests.runner.test_app_sessions_native import _FakeProcessManager, _ScriptedHarnessClient


@pytest.mark.asyncio
async def test_recreate_repl_terminal_without_terminal_registry_returns_none(
    tmp_path,
) -> None:
    """REPL recreation returns None when the resource registry has no terminal registry."""
    from fastapi.testclient import TestClient

    conv = f"conv_no_treg_{uuid.uuid4().hex[:8]}"
    resource_registry = SessionResourceRegistry(terminal_registry=None)
    resource_registry._terminal_roles[(conv, "terminal_tui_main")] = OMNIGENT_REPL_TERMINAL_ROLE

    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("tui", "main", tmp_path, running=False)
    terminal_registry._by_conversation.setdefault(conv, {})[("tui", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
    )

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"/v1/sessions/{conv}/resources/terminals/terminal_tui_main/attach"
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4404


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_when_resolver_raises_returns_no_spec() -> None:
    """Cold MCP execute tolerates spec_resolver exceptions and returns a JSON error."""
    conv = f"conv_mcp_resolver_exc_{uuid.uuid4().hex[:8]}"
    mcp = _FakeMcpManager(call_output="unused")

    async def _boom_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        raise RuntimeError("resolver boom")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_boom_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=mcp,
    )
    _session_agent_ids_ref[conv] = "ag_mcp"

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "srv__search", "arguments": {"q": "z"}},
                },
            )
    finally:
        _session_agent_ids_ref.pop(conv, None)

    assert resp.status_code == 200
    assert "No spec available" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_without_cached_spec_returns_no_spec() -> None:
    """MCP execute returns a JSON error when no spec can be resolved for the session."""
    conv = f"conv_mcp_no_spec_{uuid.uuid4().hex[:8]}"
    mcp = _FakeMcpManager(call_output="unused")
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=mcp,
    )
    _session_agent_ids_ref[conv] = "ag_missing"

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "srv__search", "arguments": {"q": "z"}},
                },
            )
    finally:
        _session_agent_ids_ref.pop(conv, None)

    assert resp.status_code == 200
    assert "No spec available" in resp.json()["error"]["message"]