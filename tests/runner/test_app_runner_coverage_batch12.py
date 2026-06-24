"""Batch-12 coverage for relay edges, bg-turn paths, and filesystem branches."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _apply_sandbox_override_from_verdict,
    _session_histories_ref,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)
from tests.runner.test_comment_relay import _StubResourceRegistry, _TOOL_RELAY_FILE


@pytest.mark.asyncio
async def test_run_turn_bg_empty_history_uses_inbound_content() -> None:
    """Fire-and-forget turns keep inbound content when history load is empty."""
    conv = f"conv_bg_empty_{uuid.uuid4().hex[:8]}"
    inbound = [{"type": "input_text", "text": "ONLY_INBOUND"}]
    spec = AgentSpec(
        spec_version=1,
        name="bg-empty-hist",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_bg"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_bg"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    _session_histories_ref.pop(conv, None)

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_bg",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": inbound,
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)

    assert hc.posted_bodies
    assert hc.posted_bodies[0]["content"] == [
        {"type": "message", "role": "user", "content": inbound},
    ]


@pytest.mark.asyncio
async def test_run_turn_bg_mcp_schema_resolution_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Background turns log MCP schema failures and still complete."""
    conv = f"conv_bg_mcp_{uuid.uuid4().hex[:8]}"
    spec = AgentSpec(
        spec_version=1,
        name="bg-mcp-fail",
        mcp_servers=[{"name": "srv", "url": "http://mcp.example"}],
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_bm"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_bm"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    class _ExplodingProxy:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def schemas_for(self, _spec: AgentSpec) -> McpSchemasResult:
            raise RuntimeError("bg schemas_for exploded")

    monkeypatch.setattr("omnigent.runner.app.ProxyMcpManager", _ExplodingProxy)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_bg_mcp",
                    "model": "test",
                    "harness": "openai-agents",
                    "has_mcp_servers": True,
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)

    assert "MCP schema resolution failed" in caplog.text
    assert hc.posted_bodies


@pytest.mark.asyncio
async def test_relay_executor_wraps_plain_text_mcp_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay tool executor wraps non-JSON MCP text in a result dict."""
    import omnigent.claude_native_bridge as _bridge_mod

    captured_executors: list[Any] = []
    real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        captured_executors.append(kwargs["tool_executor"])
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)

    class _PlainTextApClient:
        async def get(self, url: str, *, timeout: float = 10.0) -> httpx.Response:
            del timeout
            req = httpx.Request("GET", f"http://ap-server{url}")
            return httpx.Response(200, json={"labels": {}}, request=req)

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: float = 60.0,
        ) -> httpx.Response:
            del json, timeout
            req = httpx.Request("POST", f"http://ap-server{url}")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "content": [{"type": "text", "text": "plain tool output"}],
                        "isError": False,
                    }
                },
                request=req,
            )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    try:
        app = create_runner_app(
            resource_registry=_StubResourceRegistry(tmp_path),
            server_client=_PlainTextApClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert resp.status_code == 200

        assert len(captured_executors) == 1
        result = await captured_executors[0]("list_comments", {"status": "pending"})
        assert result == {"result": "plain tool output"}
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_start_failure_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed start_tool_relay must not break terminal launch."""
    import omnigent.claude_native_bridge as _bridge_mod

    def _raise_relay(**_kwargs: Any) -> None:
        raise OSError("relay bind failed")

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _raise_relay)

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            app = create_runner_app(
                resource_registry=_StubResourceRegistry(tmp_path),
                server_client=NullServerClient(),  # type: ignore[arg-type]
            )
            async with _runner_client(app) as client:
                resp = await client.post(
                    f"/v1/sessions/{session_id}/resources/terminals",
                    json={
                        "terminal": "claude",
                        "session_key": "main",
                        "bridge_inject_dir": True,
                    },
                )
                assert resp.status_code == 200

        assert "Failed to start comment relay" in caplog.text
        assert not (bridge_dir / _TOOL_RELAY_FILE).exists()
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_without_resolvable_spec_uses_fallback_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay wiring falls back to read/discovery tools when no spec is available."""
    import omnigent.claude_native_bridge as _bridge_mod

    captured_tools: list[list[dict[str, Any]]] = []
    real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        captured_tools.append(kwargs["tools"])
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    try:
        app = create_runner_app(
            resource_registry=_StubResourceRegistry(tmp_path),
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert resp.status_code == 200

        assert captured_tools
        names = {tool["name"] for tool in captured_tools[0]}
        assert "list_comments" in names
        assert "sys_session_list" in names
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_os_env_setup_failure_continues_without_os_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Relay setup logs and continues when OSEnvironment creation fails."""
    import omnigent.claude_native_bridge as _bridge_mod

    captured_tools: list[list[dict[str, Any]]] = []
    real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        captured_tools.append(kwargs["tools"])
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)
    monkeypatch.setattr(
        "omnigent.inner.os_env.create_os_environment",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("os env boom")),
    )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    try:
        with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
            app = create_runner_app(
                resource_registry=_StubResourceRegistry(tmp_path),
                server_client=NullServerClient(),  # type: ignore[arg-type]
            )
            async with _runner_client(app) as client:
                resp = await client.post(
                    f"/v1/sessions/{session_id}/resources/terminals",
                    json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
                )
                assert resp.status_code == 200

        assert captured_tools
        names = {tool["name"] for tool in captured_tools[0]}
        assert "sys_os_read" not in names
        assert "Could not create OSEnvironment for relay OS tool schemas" in caplog.text
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_filesystem_read_returns_base64_for_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Binary filesystem reads are base64-encoded in the API response."""
    conv = f"conv_fs_bin_{uuid.uuid4().hex[:8]}"
    ws = tmp_path / "workspace"
    ws.mkdir()
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(ws),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    registry = SessionResourceRegistry()
    registry._primary_envs[conv] = os_env

    from omnigent.entities.environment_filesystem import FileContent

    async def _read_binary(self: object, path: str, **kwargs: object) -> FileContent:
        del self, path, kwargs
        return FileContent(
            path="data.bin",
            data=b"\x00\x01\xff",
            bytes=3,
            truncated=False,
            encoding=None,
        )

    monkeypatch.setattr(
        "omnigent.runner.environment_filesystem.CallerProcessFilesystem.read",
        _read_binary,
    )

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/{conv}/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/filesystem/data.bin"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["encoding"] == "base64"
    assert body["content"] == "AAH/"


@pytest.mark.asyncio
async def test_per_session_fs_registry_uses_isolated_workspace_root(
    tmp_path: Path,
) -> None:
    """Sessions with a distinct workspace get a dedicated filesystem registry."""
    conv = f"conv_fs_iso_{uuid.uuid4().hex[:8]}"
    runner_ws = tmp_path / "shared_runner"
    runner_ws.mkdir()
    session_ws = tmp_path / "worktree_session"
    session_ws.mkdir()

    class _WorkspaceServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if f"/sessions/{conv}" in url:
                payload = {
                    "id": conv,
                    "agent_id": "ag_ws",
                    "workspace": str(session_ws),
                }

                class _Resp(NullServerClient._Response):
                    def json(self) -> dict[str, Any]:
                        return payload

                return _Resp()
            return await super().get(url, **kwargs)

    spec = AgentSpec(
        spec_version=1,
        name="fs-iso",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(session_ws),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        runner_workspace=runner_ws,
        spec_resolver=_resolver,
        server_client=_WorkspaceServer(),  # type: ignore[arg-type]
    )

    changes_url = (
        f"/v1/sessions/{conv}/resources/environments/"
        f"{DEFAULT_ENVIRONMENT_ID}/changes"
    )
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_ws"})
        first = await client.get(changes_url)
        second = await client.get(changes_url)

    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_interrupt_with_no_active_turn_returns_204() -> None:
    """Interrupt on an idle session is a no-op (no active turn to cancel)."""
    conv = f"conv_idle_int_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv}/events", json={"type": "interrupt"})

    assert resp.status_code == 204


def test_apply_sandbox_override_creates_os_env_when_missing() -> None:
    """Sandbox overrides materialize os_env and sandbox objects when absent."""
    spec = AgentSpec(spec_version=1, name="sandbox-materialize")
    _apply_sandbox_override_from_verdict(
        spec,
        {"arguments": {"sandbox": {"type": "linux_bwrap", "allow_network": False}}},
    )
    assert spec.os_env is not None
    assert spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.type == "linux_bwrap"
    assert spec.os_env.sandbox.allow_network is False


