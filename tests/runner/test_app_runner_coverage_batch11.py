"""Batch-11 coverage for policy gates, filesystem errors, and dispatch edges."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.entities.environment_filesystem import FileTooLarge, UnsupportedMediaType
from omnigent.entities.session_resources import SessionResourceView
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    ResolvedSpec,
    _apply_sandbox_override_from_verdict,
    _build_spawn_env_from_spec,
    _session_event_queues_ref,
    _session_histories_ref,
    _session_inboxes_ref,
)
from omnigent.runner.policy import PolicyVerdict
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)
from tests.runner.test_native_subagent_harness_resolution import (
    PARENT_AGENT_ID,
    SUB_AGENT_NAME,
    _polly_spec_tree,
)


@pytest.mark.asyncio
async def test_create_session_returns_403_when_agent_start_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /v1/sessions maps policy deny verdicts to agent_start_denied."""
    spec = AgentSpec(
        spec_version=1,
        name="deny-me",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _deny_gate(_spec: object, _harness: str) -> PolicyVerdict:
        del _spec, _harness
        return PolicyVerdict(action="deny", deny_text="sandbox policy blocked start")

    monkeypatch.setattr(runner_app_mod, "_evaluate_agent_start_gate", _deny_gate)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_start_denied", "agent_id": "ag_1"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"] == "agent_start_denied"


@pytest.mark.asyncio
async def test_create_session_swaps_sub_agent_spec_with_workdir(
    tmp_path: Path,
) -> None:
    """POST /v1/sessions with sub_agent_name spawns the child's harness."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    parent = _polly_spec_tree()

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=parent, workdir=bundle)

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "conv_child_swap",
                "agent_id": PARENT_AGENT_ID,
                "sub_agent_name": SUB_AGENT_NAME,
            },
        )

    assert resp.status_code == 201, resp.text
    assert pm.get_client_calls
    assert pm.get_client_calls[0][1] == "claude-native"


@pytest.mark.asyncio
async def test_skill_resolve_invalid_json_returns_400() -> None:
    """POST /skills/resolve returns 400 when the body is not JSON."""
    spec = AgentSpec(spec_version=1, name="skills")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/sessions/conv_skill_bad/skills/resolve",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_filesystem_read_file_too_large_returns_413(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ResourceError handler maps FileTooLarge to HTTP 413."""
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
    registry._primary_envs["conv_big"] = os_env

    async def _raise_too_large(self: object, path: str, **kwargs: object) -> object:
        del self, path, kwargs
        raise FileTooLarge("file exceeds limit")

    monkeypatch.setattr(
        "omnigent.runner.environment_filesystem.CallerProcessFilesystem.read",
        _raise_too_large,
    )

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/conv_big/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/filesystem/huge.bin"
        )

    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_filesystem_read_unsupported_media_type_returns_415(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ResourceError handler maps UnsupportedMediaType to HTTP 415."""
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
    registry._primary_envs["conv_media"] = os_env

    async def _raise_unsupported(self: object, path: str, **kwargs: object) -> object:
        del self, path, kwargs
        raise UnsupportedMediaType("binary file")

    monkeypatch.setattr(
        "omnigent.runner.environment_filesystem.CallerProcessFilesystem.read",
        _raise_unsupported,
    )

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/conv_media/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/filesystem/data.bin"
        )

    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_filesystem_diff_rejects_empty_path(tmp_path: Path) -> None:
    """Diff endpoint rejects diffing the environment root."""
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
    registry._primary_envs["conv_diff"] = os_env

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/conv_diff/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/diff/"
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_shell_invalid_timeout_returns_400(tmp_path: Path) -> None:
    """Shell endpoint rejects non-integer timeout values."""
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
    registry._primary_envs["conv_shell"] = os_env

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/conv_shell/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/shell",
            json={"command": "echo hi", "timeout": "slow"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_terminal_transfer_requires_target_session_id() -> None:
    """Terminal transfer rejects bodies missing target_session_id."""
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_src/resources/terminals/terminal_x/transfer",
            json={},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_terminal_transfer_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal transfer surfaces registry RuntimeError as 409."""
    registry = SessionResourceRegistry(terminal_registry=TerminalRegistry())

    async def _conflict(
        self: object,
        *,
        source_session_id: str,
        target_session_id: str,
        terminal_id: str,
    ) -> None:
        del self, source_session_id, target_session_id, terminal_id
        raise RuntimeError("target already owns a terminal")

    monkeypatch.setattr(SessionResourceRegistry, "transfer_terminal", _conflict)

    app = create_runner_app(
        resource_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_src/resources/terminals/terminal_x/transfer",
            json={"target_session_id": "conv_dst"},
        )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "resource_conflict"


@pytest.mark.asyncio
async def test_terminal_transfer_missing_resource_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal transfer returns 404 when the registry has no matching terminal."""
    registry = SessionResourceRegistry(terminal_registry=TerminalRegistry())

    async def _missing(
        self: object,
        *,
        source_session_id: str,
        target_session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        del self, source_session_id, target_session_id, terminal_id
        return None

    monkeypatch.setattr(SessionResourceRegistry, "transfer_terminal", _missing)

    app = create_runner_app(
        resource_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_src/resources/terminals/terminal_x/transfer",
            json={"target_session_id": "conv_dst"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_turn_setup_failure_publishes_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup-phase exceptions end the turn with session.status failed."""
    conv = "conv_setup_fail"
    spec = AgentSpec(
        spec_version=1,
        name="setup-fail",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    spawn_calls = 0
    real_build = runner_app_mod._build_spawn_env_from_spec

    def _spawn_env_after_session(*args: object, **kwargs: object) -> dict[str, str] | None:
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls <= 1:
            return real_build(*args, **kwargs)  # type: ignore[misc]
        raise RuntimeError("spawn env broke")

    monkeypatch.setattr(runner_app_mod, "_build_spawn_env_from_spec", _spawn_env_after_session)

    try:
        async with _runner_client(app) as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "hi"}],
                    },
                )
            ).status_code == 202

            failed = None
            for _ in range(200):
                while not queue.empty():
                    evt = queue.get_nowait()
                    if isinstance(evt, dict) and evt.get("status") == "failed":
                        failed = evt
                        break
                if failed is not None:
                    break
                await asyncio.sleep(0.02)
    finally:
        _session_event_queues_ref.pop(conv, None)

    assert failed is not None
    assert "turn setup failed" in failed["error"]["message"]


@pytest.mark.asyncio
async def test_empty_server_history_uses_message_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When history load is empty, the harness body keeps the inbound content."""
    conv = "conv_empty_hist"
    spec = AgentSpec(
        spec_version=1,
        name="hist-empty",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_eh"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_eh"}}),
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

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_1",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "PAYLOAD"}],
            },
        )
        assert resp.status_code == 200
        async for _ in resp.aiter_text():
            pass

    assert hc.posted_bodies
    content = hc.posted_bodies[0].get("content", [])
    assert content == [{"type": "input_text", "text": "PAYLOAD"}]


def _broken_tool_manager_cls() -> type:
    class _BrokenToolManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def get_tool_schemas(self) -> list[dict[str, Any]]:
            raise RuntimeError("schema build exploded")

    return _BrokenToolManager


@pytest.mark.asyncio
async def test_streaming_builtin_schema_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stream=true turns log _spec_builtin_tool_schemas failures and continue."""
    conv = "conv_stream_toolmgr_fail"
    spec = AgentSpec(
        spec_version=1,
        name="toolmgr-fail",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_stm"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_stm"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    monkeypatch.setattr(
        "omnigent.tools.manager.ToolManager",
        _broken_tool_manager_cls(),
    )

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
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
            resp = await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_1",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    assert "streaming builtin schema build failed" in caplog.text
    assert hc.posted_bodies


@pytest.mark.asyncio
async def test_tool_manager_schema_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fire-and-forget turns log ToolManager schema failures and still run."""
    conv = "conv_toolmgr_fail"
    spec = AgentSpec(
        spec_version=1,
        name="toolmgr-fail",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_tm"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_tm"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    monkeypatch.setattr(
        "omnigent.tools.manager.ToolManager",
        _broken_tool_manager_cls(),
    )

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
                    "agent_id": "ag_1",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)

    assert "ToolManager schema build failed" in caplog.text
    assert hc.posted_bodies


@pytest.mark.asyncio
async def test_stream_mode_harness_spawn_failure_returns_json() -> None:
    """stream=true turns surface non-streaming harness errors as JSON 503."""
    conv = "conv_stream_spawn_fail"
    spec = AgentSpec(
        spec_version=1,
        name="spawn-fail",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    class _SpawnFailPM(_FakeProcessManager):
        async def get_client(
            self,
            conversation_id: str,
            harness: str,
            env: dict[str, str] | None = None,
        ) -> _ScriptedHarnessClient:
            del harness, env
            self._sessions.add(conversation_id)
            raise RuntimeError("harness subprocess failed to start")

    app = create_runner_app(
        process_manager=_SpawnFailPM(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_1",
                "model": "test",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "harness_spawn_failed"


@pytest.mark.asyncio
async def test_native_interrupt_logs_undelivered_subagent_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native interrupt warns when sub-agent delivery cannot be confirmed."""
    conv = "conv_native_int_warn"
    native_spec = AgentSpec(
        spec_version=1,
        name="claude-int",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    def _noop_inject(_bridge_dir: object, *, timeout_s: float = 1.0) -> None:
        del _bridge_dir, timeout_s

    monkeypatch.setattr("omnigent.claude_native_bridge.inject_interrupt", _noop_inject)

    work_entry = runner_app_mod.register_subagent_work(
        parent_session_id="conv_parent_native",
        child_session_id=conv,
        agent="worker",
        title="interrupt",
    )

    def _undelivered(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
    ) -> runner_app_mod._SubagentDeliveryAck:
        del status, output
        return runner_app_mod._SubagentDeliveryAck(
            entry=work_entry,
            delivered=False,
            delivered_now=False,
            reason="missing_parent_inbox",
        )

    monkeypatch.setattr(runner_app_mod, "mark_subagent_work_terminal", _undelivered)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            async with _runner_client(app) as client:
                await client.post(
                    "/v1/sessions",
                    json={
                        "session_id": conv,
                        "agent_id": "ag_1",
                        "sub_agent_name": "worker",
                    },
                )
                resp = await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={"type": "interrupt"},
                )
                assert resp.status_code == 204
    finally:
        runner_app_mod.unregister_subagent_work(conv)

    assert "Native interrupt: sub-agent delivery not confirmed" in caplog.text


@pytest.mark.asyncio
async def test_stop_claude_native_logs_close_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Stop handler logs close_terminal failures without aborting shutdown."""
    conv = "conv_stop_close_fail"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    await terminal_registry.launch(conv, "claude", "main", instance)
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    async def _raise_close(self: object, session_id: str, terminal_id: str) -> bool:
        del self, session_id, terminal_id
        raise OSError("close failed")

    monkeypatch.setattr(SessionResourceRegistry, "close_terminal", _raise_close)

    native_spec = AgentSpec(
        spec_version=1,
        name="claude-stop",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        resource_registry=registry,
    )

    def _noop_kill(_bridge_dir: object, *, timeout_s: float = 1.0) -> None:
        del _bridge_dir, timeout_s

    monkeypatch.setattr("omnigent.claude_native_bridge.kill_session", _noop_kill)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_1"})
            await client.post(
                f"/v1/sessions/{conv}/events",
                json={"type": "stop_session"},
            )

    assert "Failed to close terminal" in caplog.text


def test_apply_sandbox_override_from_verdict_edge_cases() -> None:
    """_apply_sandbox_override_from_verdict ignores malformed verdict payloads."""
    spec = AgentSpec(spec_version=1, name="override")
    _apply_sandbox_override_from_verdict(spec, "not-a-dict")
    _apply_sandbox_override_from_verdict(spec, {"arguments": "bad"})
    _apply_sandbox_override_from_verdict(spec, {"arguments": {"sandbox": "bad"}})
    assert spec.os_env is None

    spec2 = AgentSpec(
        spec_version=1,
        name="override2",
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
    )
    _apply_sandbox_override_from_verdict(
        spec2,
        {"arguments": {"sandbox": {"type": "linux_bwrap", "allow_network": True}}},
    )
    assert spec2.os_env is not None
    assert spec2.os_env.sandbox is not None
    assert spec2.os_env.sandbox.type == "linux_bwrap"
    assert spec2.os_env.sandbox.allow_network is True


def test_build_spawn_env_for_cursor_and_antigravity(tmp_path: Path) -> None:
    """Module-level spawn-env builder covers cursor and antigravity harnesses."""
    cursor_spec = AgentSpec(
        spec_version=1,
        name="cursor-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor"}),
    )
    anti_spec = AgentSpec(
        spec_version=1,
        name="anti-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity"}),
    )
    cursor_env = _build_spawn_env_from_spec(cursor_spec, "cursor", workdir=tmp_path)
    anti_env = _build_spawn_env_from_spec(anti_spec, "antigravity")
    assert cursor_env is not None
    assert anti_env is not None


@pytest.mark.asyncio
async def test_catch_up_scan_logs_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconnect catch-up scan logs and continues when history fetch fails."""
    conv = "conv_catchup_fail"
    spec = AgentSpec(
        spec_version=1,
        name="catchup",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    _session_histories_ref[conv] = [{"type": "message", "role": "user", "content": []}]

    class _FailItemsServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if url.endswith("/items"):
                raise httpx.ConnectError("items down")
            return await super().get(url, **kwargs)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FailItemsServer(),  # type: ignore[arg-type]
    )

    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            catch_up = app.state.catch_up_scan
            await catch_up()
    finally:
        _session_histories_ref.pop(conv, None)

    assert "Catch-up scan failed" in caplog.text