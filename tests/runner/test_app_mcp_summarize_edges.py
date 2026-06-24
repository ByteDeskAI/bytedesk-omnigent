"""MCP execute, summarize, and additional runner.app route edge coverage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.runner.app import _session_agent_ids_ref, _session_event_queues_ref, _session_histories_ref
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata
from omnigent.spec.types import AgentSpec, ApiKeyAuth, CompactionConfig, DatabricksAuth, ExecutorSpec, ProviderAuth
from omnigent.tools.mcp import McpElicitationRequired
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import (
    _FakeProcessManager,
    _PaginatedServerClient,
    _ScriptedHarnessClient,
    _sse,
)


class _FakeMcpManager:
    """Minimal MCP manager stub for mcp_execute route tests."""

    def __init__(
        self,
        *,
        schemas: McpSchemasResult | None = None,
        call_output: str = "mcp-ok",
        call_error: Exception | None = None,
        schemas_error: Exception | None = None,
        elicitation: McpElicitationRequired | None = None,
        elicitation_retry_output: str = "elicited-ok",
    ) -> None:
        self._schemas = schemas or McpSchemasResult(
            schemas=[{"name": "srv__search"}],
            tool_names=["srv__search"],
            failures={},
        )
        self._call_output = call_output
        self._call_error = call_error
        self._schemas_error = schemas_error
        self._elicitation = elicitation
        self._elicitation_retry_output = elicitation_retry_output
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        del spec
        if self._schemas_error is not None:
            raise self._schemas_error
        return self._schemas

    async def call_tool(
        self,
        spec: AgentSpec,
        bare_tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        del spec, session_id
        self.call_tool_calls.append((bare_tool, arguments))
        if self._elicitation is not None:
            raise self._elicitation
        if self._call_error is not None:
            raise self._call_error
        return self._call_output

    def _resolve_owning_server(self, spec: AgentSpec, bare_tool: str) -> Any:
        del spec, bare_tool
        conn = MagicMock()
        conn.connection = MagicMock()
        conn.connection.call_tool_with_elicitation = AsyncMock(
            return_value=self._elicitation_retry_output,
        )
        return conn


@pytest.fixture
async def runner_client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_runner_app(server_client=NullServerClient()))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _make_app(
    *,
    mcp_manager: _FakeMcpManager | None = None,
    spec: AgentSpec | None = None,
    resolver_raises: bool = False,
) -> FastAPI:
    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        if resolver_raises:
            raise RuntimeError("resolver boom")
        assert spec is not None
        return spec

    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver if spec is not None else None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=mcp_manager,
    )


# ── MCP execute ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_execute_invalid_json_returns_parse_error() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv_bad_json/mcp/execute",
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_mcp_execute_unknown_method_returns_not_found() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv1/mcp/execute",
            json={"method": "ping", "params": {}},
        )
    assert resp.status_code == 200
    assert "Method not found" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_execute_tools_list_without_manager_returns_503() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv1/mcp/execute",
            json={"method": "tools/list", "params": {}},
        )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_execute_tools_list_without_spec_returns_error() -> None:
    mcp = _FakeMcpManager()
    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=mcp,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv_no_spec/mcp/execute",
            json={"method": "tools/list", "params": {}},
        )
    assert resp.status_code == 200
    assert "No spec available" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_execute_tools_list_via_spec_resolver() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager()
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_list_resolver"
    _session_agent_ids_ref[conv] = "ag_mcp"

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={"method": "tools/list", "params": {}},
            )
    finally:
        _session_agent_ids_ref.pop(conv, None)

    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["tool_names"] == ["srv__search"]
    assert body["schemas"]


@pytest.mark.asyncio
async def test_mcp_execute_tools_list_schemas_for_failure() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager(schemas_error=RuntimeError("schema boom"))
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_list_fail"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={"method": "tools/list", "params": {}},
        )

    assert resp.status_code == 200
    assert "error" in resp.json()
    assert resp.json()["error"]["code"] == -32000


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_missing_name() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv1/mcp/execute",
            json={"method": "tools/call", "params": {"arguments": {}}},
        )
    assert resp.status_code == 200
    assert resp.json()["error"]["message"] == "Missing tool name"


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_namespaced_without_manager_returns_503() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv1/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": "srv__search", "arguments": {}},
            },
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_namespaced_happy_path() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager(call_output="found it")
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_call"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": "srv__search", "arguments": {"q": "x"}},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["result"]["output"] == "found it"
    assert mcp.call_tool_calls == [("search", {"q": "x"})]


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_elicitation_required() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    elicit = McpElicitationRequired(
        input_requests={"req-1": {"message": "approve?"}},
        request_state="state-abc",
        tool_name="search",
        arguments={},
    )
    mcp = _FakeMcpManager(elicitation=elicit)
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_elicit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": "srv__search", "arguments": {}},
            },
        )

    body = resp.json()["result"]["input_required"]
    assert body["requestState"] == "state-abc"
    assert body["inputRequests"]["req-1"]["message"] == "approve?"


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_elicitation_retry_with_input_responses() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager(elicitation_retry_output="after-approval")
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_elicit_retry"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "srv__search",
                    "arguments": {"q": "y"},
                    "inputResponses": {"req-1": {"action": "accept"}},
                    "requestState": "state-xyz",
                },
            },
        )

    assert resp.status_code == 200
    assert resp.json()["result"]["output"] == "after-approval"


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_mcp_dispatch_error() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager(call_error=RuntimeError("call failed"))
    app = _make_app(mcp_manager=mcp, spec=spec)
    conv = "conv_mcp_call_err"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": "srv__search", "arguments": {}},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32000


@pytest.mark.asyncio
async def test_mcp_execute_tools_call_runner_local_dispatch_error() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    app = _make_app(spec=spec)
    conv = "conv_local_err"

    async def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("local tool boom")

    orig = tool_dispatch.execute_tool
    tool_dispatch.execute_tool = _boom

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "sys_os_read", "arguments": {"path": "/"}},
                },
            )
    finally:
        tool_dispatch.execute_tool = orig

    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32000


@pytest.mark.asyncio
async def test_mcp_execute_tools_list_spec_resolver_failure_still_errors() -> None:
    spec = AgentSpec(spec_version=1, name="mcp-agent")
    mcp = _FakeMcpManager()
    app = _make_app(mcp_manager=mcp, spec=spec, resolver_raises=True)
    conv = "conv_resolver_fail"
    _session_agent_ids_ref[conv] = "ag_mcp"

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={"method": "tools/list", "params": {}},
            )
    finally:
        _session_agent_ids_ref.pop(conv, None)

    assert resp.status_code == 200
    assert "No spec available" in resp.json()["error"]["message"]


# ── Summarize + auth resolution ───────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_invalid_body_returns_400() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post("/v1/summarize", json={"messages": "not-a-list"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_summarize_resolves_api_key_auth_from_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    spec = AgentSpec(
        spec_version=1,
        name="summarize-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            auth=ApiKeyAuth(api_key="sk-test", base_url="https://gateway.example/v1"),
        ),
    )

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="summary out")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_apikey"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_sum"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["text"] == "summary out"
    assert captured["connection"] == {
        "api_key": "sk-test",
        "base_url": "https://gateway.example/v1",
    }


@pytest.mark.asyncio
async def test_summarize_resolves_provider_auth_via_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="summarize-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            auth=ProviderAuth(name="litellm"),
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="prov summary")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )

    # _resolve_provider_connection is closure-local; patch its dependency instead.
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_providers",
        lambda _cfg: {
            "litellm": SimpleNamespace(
                kind="key",
                profile=None,
                family=lambda name: SimpleNamespace(
                    api_key="prov-key",
                    base_url="https://litellm.example/v1",
                )
                if name == "openai"
                else None,
            ),
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_provider"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_sum"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"]["api_key"] == "prov-key"


@pytest.mark.asyncio
async def test_summarize_resolves_databricks_auth_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="summarize-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="databricks/databricks-gpt-5",
            auth=DatabricksAuth(profile="oss"),
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="db summary")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host="https://dbc.example", token="db-token"),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_db"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_sum"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "databricks/databricks-gpt-5",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"]["api_key"] == "db-token"
    assert captured["connection"]["base_url"] == "https://dbc.example/serving-endpoints"


# ── Events: harness lookup + validation ───────────────────────────────


@pytest.mark.asyncio
async def test_events_steering_returns_503_when_harness_missing() -> None:
    class _NoHarnessPM(_FakeProcessManager):
        async def get_client(
            self,
            conversation_id: str,
            harness: str,
            env: dict[str, str] | None = None,
        ) -> _ScriptedHarnessClient:
            del conversation_id, harness, env
            raise RuntimeError("no harness for session")

    spec = AgentSpec(
        spec_version=1,
        name="steer-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    conv = "conv_no_harness_steering"
    app = create_runner_app(
        process_manager=_NoHarnessPM(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={"type": "steering", "content": [{"type": "input_text", "text": "wait"}]},
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "no_harness"


@pytest.mark.asyncio
async def test_events_model_change_rejects_non_string_model_on_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import claude_native_bridge
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", lambda *_a, **_k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_bad_model_type"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={"type": "model_change", "model": 42},
        )

    assert resp.status_code == 400
    assert "must be a string" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_events_effort_change_rejects_non_string_effort_on_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import claude_native_bridge
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", lambda *_a, **_k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_bad_effort_type"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={"type": "effort_change", "effort": {"level": "high"}},
        )

    assert resp.status_code == 400
    assert "must be a string" in resp.json()["detail"]


# ── Compaction: provider_tokens + layer-2 serialize branches ──────────


@pytest.mark.asyncio
async def test_proactive_compaction_uses_provider_tokens_from_prior_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = "conv_provider_tokens"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={"harness": "openai-agents", "model": "gpt-4o-mini"},
        ),
    )
    sse_turn1 = [
        _sse({"type": "response.created", "response": {"id": "resp_pt1"}}),
        _sse(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_pt1",
                    "usage": {"context_tokens": 70000},
                },
            },
        ),
    ]
    sse_turn2 = [
        _sse({"type": "response.created", "response": {"id": "resp_pt2"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_pt2"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_turn1 + sse_turn2)
    pm = _FakeProcessManager(harness_client)
    compact_calls: list[int] = []

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one"}]},
    ] * 10

    # If provider_tokens were ignored, this low estimate would skip compaction.
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 100,
    )

    async def _fake_compact(messages: Any, *_args: Any, **_kwargs: Any) -> CompactionResult:
        compact_calls.append(len(messages))
        return CompactionResult(
            messages=messages[:2],
            summary_metadata=SummaryMetadata(
                text="compact via provider tokens",
                last_item_id="item_pt",
                model="gpt-4o-mini",
                token_count=5,
            ),
            total_tokens=5,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: MagicMock(),
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            assert (await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "gpt-4o-mini",
                        "content": [{"type": "input_text", "text": "turn1"}],
                    },
                )
            ).status_code == 202

            for _ in range(300):
                if len(harness_client.posted_bodies) >= 1:
                    break
                await asyncio.sleep(0.01)

            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "gpt-4o-mini",
                        "content": [{"type": "input_text", "text": "turn2"}],
                    },
                )
            ).status_code == 202

            for _ in range(300):
                if compact_calls:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)

    assert compact_calls, "expected proactive compaction on turn 2 using provider_tokens"


@pytest.mark.asyncio
async def test_proactive_compaction_layer2_serializes_str_content_and_truncates_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = "conv_layer2_str"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={"harness": "openai-agents"},
        ),
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_l2s"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_l2s"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    compaction_posts: list[dict[str, Any]] = []
    history_items = [{"id": "item_l2s", "type": "message", "role": "user", "content": []}]

    class _RecordingServerClient(_PaginatedServerClient):
        def __init__(self) -> None:
            super().__init__(history_items)

        async def post(self, url: str, **kwargs: Any) -> Any:
            if url.endswith("/events"):
                compaction_posts.append(kwargs.get("json", {}))
            return await NullServerClient().post(url, **kwargs)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_RecordingServerClient(),  # type: ignore[arg-type]
    )

    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": "plain-string-content"},
        {
            "type": "message",
            "role": "assistant",
            "content": ["block-as-string", {"type": "output_text", "text": "hi"}],
        },
    ] * 40

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 70000,
    )

    long_output = "z" * 300

    async def _layer2_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        "inline-string-block",
                        {"type": "output_text", "text": "nested"},
                    ],
                },
                {"type": "function_call", "name": "tool_x"},
                {"type": "function_call_output", "output": long_output},
            ],
            summary_metadata=None,
            total_tokens=3,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _layer2_compact)
    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: MagicMock(),
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            assert (await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "gpt-4o-mini",
                        "content": [{"type": "input_text", "text": "go"}],
                    },
                )
            ).status_code == 202

            for _ in range(300):
                if compaction_posts:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)

    compaction_event = next(
        (p for p in compaction_posts if p.get("type") == "compaction"),
        None,
    )
    assert compaction_event is not None
    summary = compaction_event.get("data", {}).get("summary", "")
    assert "inline-string-block" in summary
    assert "nested" in summary
    assert "[tool call]: tool_x" in summary
    assert "..." in summary


@pytest.mark.asyncio
async def test_proactive_compaction_resolves_api_key_connection_for_summarize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = "conv_compact_apikey"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            auth=ApiKeyAuth(api_key="sk-compact", base_url="https://gw.example/v1"),
            config={"harness": "openai-agents"},
        ),
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_ak"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_ak"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    captured_connections: list[dict[str, str] | None] = []

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    async def _fake_compact(
        messages: Any,
        *_args: Any,
        connection: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> CompactionResult:
        captured_connections.append(connection)
        return CompactionResult(
            messages=messages[:1],
            summary_metadata=SummaryMetadata(
                text="ok",
                last_item_id="item_ak",
                model="gpt-4o-mini",
                token_count=1,
            ),
            total_tokens=1,
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PaginatedServerClient([{"id": "item_ak", "type": "message", "role": "user", "content": []}]),  # type: ignore[arg-type]
    )

    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]},
    ] * 60

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 70000)
    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: MagicMock())

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
            await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "gpt-4o-mini",
                    "content": [{"type": "input_text", "text": "go"}],
                },
            )
            for _ in range(300):
                if captured_connections:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)

    assert captured_connections
    assert captured_connections[0] == {
        "api_key": "sk-compact",
        "base_url": "https://gw.example/v1",
    }


# ── Elicitation forward route ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_elicitation_route_requires_response_id() -> None:
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post("/v1/elicitations/elicit_1", json={"action": "accept"})
    assert resp.status_code == 400
    assert "response_id required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_elicitation_route_returns_404_for_unknown_response_id() -> None:
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/elicitations/elicit_missing",
            json={"action": "accept", "response_id": "resp_unknown"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_elicitation_route_forwards_approval_to_harness() -> None:
    conv = "conv_elicit_fwd"
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_elicit"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_elicit"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(
        spec_version=1,
        name="elicit-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "gpt-4o-mini",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        for _ in range(200):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.01)

        resp = await client.post(
            "/v1/elicitations/elicit_ok",
            json={
                "action": "accept",
                "content": {"approved": True},
                "response_id": "resp_elicit",
            },
        )

    assert resp.status_code == 200
    assert harness_client.posted_bodies
    forwarded = harness_client.posted_bodies[-1]
    assert forwarded["type"] == "approval"
    assert forwarded["elicitation_id"] == "elicit_ok"
    assert forwarded["content"] == {"approved": True}


@pytest.mark.asyncio
async def test_elicitation_route_returns_502_when_harness_post_fails() -> None:
    class _FailingHarnessClient(_ScriptedHarnessClient):
        async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
            del url, json, timeout
            raise httpx.ConnectError("harness down")

    conv = "conv_elicit_fail"
    harness_client = _FailingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_elicit_fail"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_elicit_fail"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(
        spec_version=1,
        name="elicit-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "gpt-4o-mini",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        for _ in range(200):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.01)

        resp = await client.post(
            "/v1/elicitations/elicit_boom",
            json={"action": "decline", "response_id": "resp_elicit_fail"},
        )

    assert resp.status_code == 502
    assert resp.json()["error"] == "elicitation_failed"