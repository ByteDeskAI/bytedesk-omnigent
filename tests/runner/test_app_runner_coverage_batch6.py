"""Batch-6 edge coverage for proxy_stream, spawn_env, and lazy spec paths."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.pi_native_bridge import build_pi_native_spawn_env
from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_histories_ref
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)


class _Non200StreamHarnessClient:
    """Harness whose stream context returns a non-200 status."""

    def __init__(self, *, status_code: int = 503) -> None:
        self.posted_bodies: list[dict[str, Any]] = []
        self._status_code = status_code

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        status_code = self._status_code

        class _StreamCtx:
            async def __aenter__(self_inner) -> Any:
                class _Handle:
                    async def aiter_text(self_handle) -> AsyncIterator[str]:
                        yield ""

                _Handle.status_code = status_code
                return _Handle()

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        _StreamCtx.status_code = status_code
        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        del url, timeout, json

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

        return _Response()


class _MalformedThenOkHarnessClient(_ScriptedHarnessClient):
    """Streams a malformed SSE frame before a successful completion."""

    def __init__(self) -> None:
        super().__init__(
            [
                _sse({"type": "response.created", "response": {"id": "resp_mal"}}),
                "data: {not valid json}\n\n",
                _sse({"type": "response.completed", "response": {"id": "resp_mal"}}),
            ]
        )


class _ToolOutputHarnessClient(_ScriptedHarnessClient):
    """Emits a function_call_output item for in-memory history coverage."""

    def __init__(self) -> None:
        super().__init__(
            [
                _sse({"type": "response.created", "response": {"id": "resp_out"}}),
                _sse(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call_output",
                            "call_id": "call_out_1",
                            "output": "tool result payload",
                        },
                    }
                ),
                _sse({"type": "response.completed", "response": {"id": "resp_out"}}),
            ]
        )


@pytest.mark.asyncio
async def test_stream_harness_non_200_emits_response_failed() -> None:
    """A harness stream returning non-200 publishes response.failed and ends failed."""
    conv = "conv_non200_stream"
    spec = AgentSpec(spec_version=1, name="non200-agent")
    hc = _Non200StreamHarnessClient(status_code=503)
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_non200",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        text = resp.text

    assert resp.status_code == 200
    assert "response.failed" in text
    assert '"status": 503' in text or '"status":503' in text


@pytest.mark.asyncio
async def test_stream_malformed_sse_json_hits_decode_guard() -> None:
    """Invalid JSON in an SSE data line is caught by the decode guard."""
    conv = "conv_malformed_sse"
    spec = AgentSpec(spec_version=1, name="malformed-sse")
    hc = _MalformedThenOkHarnessClient()
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'get'"):
            await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_mal",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )

    assert len(hc.posted_bodies) == 1


@pytest.mark.asyncio
async def test_stream_records_function_call_output_in_history() -> None:
    """function_call_output SSE items are mirrored into session history."""
    conv = "conv_fc_output"
    spec = AgentSpec(spec_version=1, name="fc-output-agent")
    hc = _ToolOutputHarnessClient()
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_out",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

        outputs = [
            h
            for h in _session_histories_ref.get(conv, [])
            if h.get("type") == "function_call_output"
        ]
        assert outputs == [
            {
                "type": "function_call_output",
                "call_id": "call_out_1",
                "output": "tool result payload",
            }
        ]
    finally:
        _session_histories_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_pi_native_turn_builds_spawn_env() -> None:
    """pi-native harness turns build spawn env when dispatch omits it."""
    conv = "conv_pi_spawn"
    spec = AgentSpec(
        spec_version=1,
        name="pi-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_pi"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_pi"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    expected_env = build_pi_native_spawn_env(conv)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        assert (
            await client.post(
                "/v1/sessions",
                json={"session_id": conv, "agent_id": "ag_pi"},
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_pi",
                    "model": "test",
                    "content": [{"type": "input_text", "text": "go"}],
                },
            )
        ).status_code == 202
        for _ in range(200):
            if pm.get_client_calls:
                break
            await asyncio.sleep(0.01)

    assert pm.get_client_calls
    _conv, harness, env = pm.get_client_calls[-1]
    assert _conv == conv
    assert harness == "pi-native"
    assert env == expected_env


@pytest.mark.asyncio
async def test_agent_version_bump_releases_harness_client() -> None:
    """A higher agent_version releases the cached harness client before respawn."""
    conv = "conv_agent_ver"
    spec = AgentSpec(spec_version=1, name="versioned-agent")
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_v1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_v1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        for version in (1, 2):
            resp = await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_ver",
                    "agent_version": version,
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": f"v{version}"}],
                },
            )
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    assert conv in pm.released
    assert len(pm.get_client_calls) == 2


@pytest.mark.asyncio
async def test_lazy_turn_spec_cache_reuses_resolver_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-agent spec cache avoids repeat resolver calls on streaming turns."""
    conv = "conv_lazy_cache"
    spec = AgentSpec(spec_version=1, name="lazy-cache-agent")
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_lc"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_lc"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del session_id
        resolver_calls += 1
        assert agent_id == "ag_lazy"
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        for _ in range(2):
            resp = await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_lazy",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "again"}],
                },
            )
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    assert resolver_calls == 1


@pytest.mark.asyncio
async def test_mcp_schema_partial_failure_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MCP schema resolution warnings are logged for per-server failures."""
    import logging

    conv = "conv_mcp_warn"
    spec = AgentSpec(
        spec_version=1,
        name="mcp-warn-agent",
        mcp_servers=[{"name": "srv", "url": "http://mcp.example"}],
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_mcp"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_mcp"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING, logger="omnigent.runner.app")

    class _ProxyWithFailures:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def schemas_for(self, _spec: AgentSpec) -> McpSchemasResult:
            return McpSchemasResult(
                schemas=[],
                tool_names=[],
                failures={"srv": "connection refused"},
            )

    monkeypatch.setattr("omnigent.runner.app.ProxyMcpManager", _ProxyWithFailures)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_mcp",
                "model": "test",
                "harness": "openai-agents",
                "has_mcp_servers": True,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200
        async for _ in resp.aiter_text():
            pass

    assert "runner MCP 'srv' unavailable for this turn: connection refused" in caplog.text


@pytest.mark.asyncio
async def test_summarize_provider_without_family_returns_none_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider auth with no resolvable API family yields None summarize connection."""
    from types import SimpleNamespace

    spec = AgentSpec(
        spec_version=1,
        name="no-family-provider",
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            auth=ProviderAuth(name="empty-family"),
        ),
    )
    captured: dict[str, Any] = {}

    class _ProviderEntry:
        kind = "openai"

        @property
        def profile(self) -> str | None:
            return None

        def family(self, _name: str) -> Any:
            return None

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="no family")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: {})
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_providers",
        lambda _cfg: {"empty-family": _ProviderEntry()},
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
    conv = "conv_no_family"

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"] is None