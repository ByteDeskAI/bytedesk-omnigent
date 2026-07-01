"""Batch-13 coverage for remaining runner.app gaps (snapshot, relay, wake, fs)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent import claude_native_bridge
from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.entities.environment_filesystem import FileContent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _build_spawn_env_from_spec,
    _native_terminal_start_error_payload,
    _publish_native_terminal_start_error,
    _session_agent_ids_ref,
    _session_event_queues_ref,
    _session_histories_ref,
    _session_inboxes_ref,
    register_subagent_work,
    unregister_subagent_work,
)
from omnigent.runner.resource_registry import OMNIGENT_REPL_TERMINAL_ROLE, SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_mcp_summarize_edges import _FakeMcpManager
from tests.runner.test_app_runner_coverage_batch8 import _NativeCreateServerClient, _native_spec
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _WakeRecordingServerClient,
    _runner_client,
    _sse,
)
from tests.runner.test_comment_relay import _StubResourceRegistry, _TOOL_RELAY_FILE
from tests.runner.test_native_subagent_harness_resolution import (
    PARENT_AGENT_ID,
    SUB_AGENT_NAME,
    _polly_spec_tree,
)


def _json_response(payload: dict[str, Any], *, status_code: int = 200) -> Any:
    """Build a minimal httpx-like response stub with a JSON body."""

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    return _Resp()


@pytest.mark.parametrize(
    "error_payload",
    [
        {"code": 123, "message": "bad"},
        {"code": "", "message": ""},
    ],
)
@pytest.mark.asyncio
async def test_child_status_failed_malformed_error_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    error_payload: dict[str, Any],
) -> None:
    """Parent fan-out ignores malformed session.status error payloads."""
    parent_id = "conv_parent_bad_err"
    child_id = "conv_child_bad_err"
    monkeypatch.setattr(
        runner_app_mod,
        "_native_terminal_start_error_payload",
        lambda *_a, **_k: error_payload,
    )

    async def _fail_create(
        session_id: str,
        _registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> None:
        _publish_native_terminal_start_error(
            publish_event,  # type: ignore[arg-type]
            session_id,
            "Claude",
            RuntimeError("bootstrap failed"),
        )
        raise RuntimeError("terminal create aborted")

    monkeypatch.setattr(runner_app_mod, "_auto_create_claude_terminal", _fail_create)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("claude-native")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )
    runner_app_mod._session_event_queues_ref.pop(parent_id, None)
    runner_app_mod.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": child_id, "agent_id": "ag_child"},
            )
        assert resp.status_code == 201
        events: list[dict[str, Any]] = []
        queue = _session_event_queues_ref.get(parent_id)
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    events.append(item)
    finally:
        runner_app_mod.unregister_child_session(child_id)
        _session_event_queues_ref.pop(parent_id, None)
        _session_event_queues_ref.pop(child_id, None)

    child_updates = [e for e in events if e.get("type") == "session.child_session.updated"]
    assert child_updates
    assert child_updates[-1]["child"].get("last_task_error") is None


@pytest.mark.asyncio
async def test_child_status_idle_fanout_uses_history_preview() -> None:
    """Child idle fan-out reads runner history for the parent preview."""
    parent_id = "conv_parent_child_preview"
    child_id = "conv_child_child_preview"
    stream_finished = asyncio.Event()
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_child_preview"}}),
            _sse({"type": "response.output_text.delta", "delta": "DELEGATE_ACK_SINGLE"}),
            _sse(
                {
                    "type": "response.completed",
                    "response": {"id": "resp_child_preview", "status": "completed"},
                }
            ),
        ],
        stream_finished=stream_finished,
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="platform-architect")

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app_mod._session_event_queues_ref.pop(parent_id, None)
    runner_app_mod._session_event_queues_ref.pop(child_id, None)
    runner_app_mod._session_histories_ref.pop(child_id, None)
    runner_app_mod.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="platform-architect:MAYA_SINGLE_DELEGATION",
        tool="platform-architect",
        session_name="MAYA_SINGLE_DELEGATION",
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": child_id, "agent_id": "ag_platform_architect"},
            )
            assert create_resp.status_code == 201
            event_resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Reply with exactly DELEGATE_ACK_SINGLE.",
                        }
                    ],
                },
            )
            assert event_resp.status_code == 202
            await asyncio.wait_for(stream_finished.wait(), timeout=2.0)
            await asyncio.sleep(0)

        events: list[dict[str, Any]] = []
        queue = _session_event_queues_ref.get(parent_id)
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    events.append(item)
    finally:
        runner_app_mod.unregister_child_session(child_id)
        runner_app_mod.unregister_subagent_work(child_id)
        _session_event_queues_ref.pop(parent_id, None)
        _session_event_queues_ref.pop(child_id, None)
        _session_histories_ref.pop(child_id, None)

    child_updates = [e for e in events if e.get("type") == "session.child_session.updated"]
    assert child_updates
    assert child_updates[-1]["child"]["last_message_preview"] == "DELEGATE_ACK_SINGLE"


@pytest.mark.asyncio
async def test_concurrent_skills_reads_share_cached_session_snapshot(
    tmp_path: Path,
) -> None:
    """Followers waiting on the snapshot lock reuse the cached result."""
    conv = f"conv_snap_recheck_{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    snapshot_count = 0
    snapshot_started = asyncio.Event()
    release = asyncio.Event()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal snapshot_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            snapshot_count += 1
            snapshot_started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": "ag_snap", "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="skills-agent")

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_resolver,
        server_client=server_client,
    )
    try:
        async with _runner_client(app) as client:
            first = asyncio.create_task(client.get(f"/v1/sessions/{conv}/skills"))
            await asyncio.wait_for(snapshot_started.wait(), timeout=5.0)
            followers = [
                asyncio.create_task(client.get(f"/v1/sessions/{conv}/skills"))
                for _ in range(4)
            ]
            release.set()
            responses = await asyncio.gather(first, *followers)
    finally:
        await server_client.aclose()

    assert all(r.status_code == 200 for r in responses)
    assert snapshot_count == 1


@pytest.mark.asyncio
async def test_session_snapshot_swallows_transport_errors(tmp_path: Path) -> None:
    """Snapshot fetch exceptions fall back without caching a broken snapshot."""
    conv = f"conv_snap_exc_{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    class _BoomServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if url.endswith(f"/sessions/{conv}"):
                raise httpx.ConnectError("snapshot down")
            return await super().get(url, **kwargs)

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    registry = SessionResourceRegistry()
    registry._primary_envs[conv] = os_env
    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=_BoomServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/{conv}/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/changes"
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_filesystem_uses_global_registry_when_workspace_matches_runner(
    tmp_path: Path,
) -> None:
    """Per-session fs registry reuses the runner-global registry for the default workspace."""
    ws = tmp_path / "shared"
    ws.mkdir()
    conv = f"conv_fs_shared_{uuid.uuid4().hex[:8]}"

    class _WorkspaceServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if url.endswith(f"/sessions/{conv}"):
                return _json_response({"workspace": str(ws), "agent_id": "ag_fs"})
            return await super().get(url, **kwargs)

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(ws),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    registry = SessionResourceRegistry()
    registry._primary_envs[conv] = os_env

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=_WorkspaceServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        first = await client.get(
            f"/v1/sessions/{conv}/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/changes"
        )
        second = await client.get(
            f"/v1/sessions/{conv}/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/changes"
        )

    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_value_error_handler_maps_to_invalid_input() -> None:
    """ValueError exceptions from resolve_environment return HTTP 400."""
    conv = "conv_val_err"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/{conv}/resources/environments/"
            f"env_does_not_exist/filesystem/foo.txt"
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_claude_native_spec_resolution_failure_continues_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Claude terminal auto-create continues when agent spec resolution fails."""
    created: list[str] = []

    async def _stub_create(session_id: str, *_args: object, **_kwargs: object) -> object:
        created.append(session_id)
        from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id

        return SessionResourceView(
            id=terminal_resource_id("claude", "main"),
            type="terminal",
            session_id=session_id,
            name="claude",
        )

    monkeypatch.setattr(runner_app_mod, "_auto_create_claude_terminal", _stub_create)

    unwrap_calls = 0
    orig_unwrap = runner_app_mod._unwrap_resolved_spec

    def _unwrap_raises_once(entry: object) -> object:
        nonlocal unwrap_calls
        unwrap_calls += 1
        if unwrap_calls == 1:
            raise OmnigentError("agent not bound", code=ErrorCode.NOT_FOUND)
        return orig_unwrap(entry)

    monkeypatch.setattr(runner_app_mod, "_unwrap_resolved_spec", _unwrap_raises_once)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("claude-native")

    sid = f"conv_claude_spec_fail_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            resp = await client.post("/v1/sessions", json={"session_id": sid, "agent_id": "ag_1"})

    assert resp.status_code == 201
    assert created == [sid]
    assert "Claude terminal spec resolution failed" in caplog.text


@pytest.mark.asyncio
async def test_codex_native_spec_resolution_failure_still_bootstraps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex terminal auto-create tolerates OmnigentError during spec resolution."""
    created: list[str] = []

    async def _stub_create(session_id: str, *_args: object, **_kwargs: object) -> object:
        created.append(session_id)
        from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id

        return SessionResourceView(
            id=terminal_resource_id("codex", "main"),
            type="terminal",
            session_id=session_id,
            name="codex",
        )

    monkeypatch.setattr(runner_app_mod, "_auto_create_codex_terminal", _stub_create)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_session_needs_runner_terminal",
        AsyncMock(return_value=True),
    )

    unwrap_calls = 0
    orig_unwrap = runner_app_mod._unwrap_resolved_spec

    def _unwrap_raises_once(entry: object) -> object:
        nonlocal unwrap_calls
        unwrap_calls += 1
        if unwrap_calls == 1:
            raise OmnigentError("agent not bound", code=ErrorCode.NOT_FOUND)
        return orig_unwrap(entry)

    monkeypatch.setattr(runner_app_mod, "_unwrap_resolved_spec", _unwrap_raises_once)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("codex-native")

    sid = f"conv_codex_spec_fail_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post("/v1/sessions", json={"session_id": sid, "agent_id": "ag_1"})

    assert resp.status_code == 201
    assert created == [sid]


@pytest.mark.asyncio
async def test_codex_native_injects_orchestrator_skills_when_bundle_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex bootstrap links orchestrator skills when a bundle workdir exists."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    injected: list[Path] = []

    def _capture_inject(bundle_dir: Path, _spec: object) -> None:
        injected.append(bundle_dir)

    monkeypatch.setattr(runner_app_mod, "_ensure_orchestrator_skills_in_bundle", _capture_inject)
    monkeypatch.setattr(runner_app_mod, "_auto_create_codex_terminal", AsyncMock())
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_session_needs_runner_terminal",
        AsyncMock(return_value=True),
    )

    spec = AgentSpec(
        spec_version=1,
        name="codex-bundle",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> object:
        del agent_id, session_id
        from omnigent.runner.app import ResolvedSpec

        return ResolvedSpec(spec=spec, workdir=bundle)

    sid = f"conv_codex_skills_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post("/v1/sessions", json={"session_id": sid, "agent_id": "ag_1"})

    assert resp.status_code == 201
    assert injected == [bundle]


@pytest.mark.asyncio
async def test_recover_sub_agent_name_swallows_snapshot_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agent name recovery returns None when snapshot construction raises."""
    from omnigent.runner.app import _SessionSnapshot as _OrigSessionSnapshot

    child_id = f"conv_child_snap_exc_{uuid.uuid4().hex[:8]}"
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_se"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_se"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    def _raising_snapshot(*_args: object, **_kwargs: object) -> _OrigSessionSnapshot:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(runner_app_mod, "_SessionSnapshot", _raising_snapshot)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="parent")

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_parent",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 202
        for _ in range(200):
            if hc.posted_bodies:
                break
            await asyncio.sleep(0.01)

    assert hc.posted_bodies


def test_terminal_attach_repop_pending_cost_popup_claude_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach re-pop reads the claude-native permission_hook config path."""
    captured: list[Path] = []

    def _fake_launch(
        _socket_path: str,
        _tmux_target: str,
        config_file: Path,
        *,
        session_id: str,
        elicitation_id: str,
        **_kwargs: object,
    ) -> None:
        del session_id, elicitation_id
        captured.append(config_file)

    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", lambda *_a, **_k: True)
    monkeypatch.setattr("omnigent.native_cost_popup.launch_cost_popup", _fake_launch)

    async def _slow_bridge(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    native_spec = _native_spec("claude-native")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    pending = [
        {
            "elicitation_id": "elicit_claude",
            "params": {
                "phase": "llm_request",
                "message": "Approve spend",
                "policy_name": "cost-cap",
            },
        }
    ]

    class _PendingServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            if (
                url.startswith(f"/v1/sessions/{conv_id}")
                and "/items" not in url
                and "/labels" not in url
            ):
                return _json_response({"pending_elicitations": pending, "agent_id": "ag_repop"})
            return await super().get(url, **kwargs)

    conv_id = f"conv_claude_repop_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PendingServer(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    client = TestClient(app)
    assert client.post(
        "/v1/sessions",
        json={"session_id": conv_id, "agent_id": "ag_repop"},
    ).status_code == 201

    with client.websocket_connect(
        f"/v1/sessions/{conv_id}/resources/terminals/terminal_claude_main/attach"
    ):
        pass

    assert len(captured) == 1
    assert captured[0].name == claude_native_bridge._PERMISSION_HOOK_FILE  # noqa: SLF001


@pytest.mark.asyncio
async def test_interrupt_after_turn_completes_is_noop_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt after the harness turn ends does not cancel a replacement turn."""
    conv = f"conv_int_done_{uuid.uuid4().hex[:8]}"
    gate = asyncio.Event()
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_fast"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_fast"}}),
        ],
        stream_finished=gate,
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_1",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "go"}],
            },
        )
        assert resp.status_code == 202
        await asyncio.wait_for(gate.wait(), timeout=5.0)
        interrupt = await client.post(f"/v1/sessions/{conv}/events", json={"type": "interrupt"})
        assert interrupt.status_code == 204


@pytest.mark.asyncio
async def test_self_parent_subagent_completion_does_not_wake_parent() -> None:
    """A session marked as its own parent never schedules a sub-agent wake."""
    session_id = f"conv_self_parent_{uuid.uuid4().hex[:8]}"
    server_client = _WakeRecordingServerClient(session_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _session_inboxes_ref[session_id] = inbox
    register_subagent_work(
        parent_session_id=session_id,
        child_session_id=session_id,
        agent="worker",
        title="self",
    )
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "SELF_DONE"},
                },
            )
        assert resp.status_code == 204
        assert server_client.wake_posts == []
    finally:
        unregister_subagent_work(session_id)
        _session_inboxes_ref.pop(session_id, None)


@pytest.mark.asyncio
async def test_relay_skips_when_concurrent_starter_already_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comment relay setup returns early when another starter wins the race."""
    import omnigent.claude_native_bridge as _bridge_mod

    start_count = 0
    real_start = _bridge_mod.start_tool_relay

    def _counting_relay(**kwargs: Any) -> Any:
        nonlocal start_count
        start_count += 1
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _counting_relay)

    session_id = "conv_relay_race"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    try:
        app = create_runner_app(
            resource_registry=_StubResourceRegistry(tmp_path),
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as client:
            first = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            second = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert first.status_code == 200
            assert second.status_code == 200
        assert start_count == 1
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_spec_resolution_error_uses_readonly_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay setup falls back to read-only tools when spec resolution fails."""
    import omnigent.claude_native_bridge as _bridge_mod

    captured_tools: list[list[dict[str, Any]]] = []
    real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        captured_tools.append(kwargs["tools"])
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)

    session_id = f"conv_relay_spec_{uuid.uuid4().hex[:12]}"
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
        names = {tool["name"] for tool in captured_tools[0]}
        assert "sys_session_list" in names
        assert "sys_session_send" not in names
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_advisor_sticky_model_applied_on_followup_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advisor-applied models stick on conversational follow-up stream turns."""
    from omnigent.cost_plan import AdvisorVerdict
    from omnigent.runner import cost_advisor as cost_advisor_mod
    from tests.runner.test_app_sessions_native import (
        _LabelPatchRecordingServerClient,
        _advisor_note_items,
        _advisor_orchestrator_spec,
    )

    conv = f"conv_sticky_{uuid.uuid4().hex[:8]}"
    judge_calls = 0

    class _PriceyThenConversationalJudge:
        """Return an expensive verdict once, then conversational silence."""

        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
            nonlocal judge_calls
            del query
            judge_calls += 1
            if judge_calls == 1:
                return AdvisorVerdict(
                    tier="expensive",
                    model="model-pricey",
                    applied=False,
                    rationale="hard work",
                    turn_anchor=turn_anchor,
                )
            return None

    def _build_stub_judge(**_kwargs: object) -> _PriceyThenConversationalJudge:
        return _PriceyThenConversationalJudge()

    monkeypatch.setattr(cost_advisor_mod, "build_llm_judge", _build_stub_judge)

    spec = _advisor_orchestrator_spec()
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
            _sse({"type": "response.created", "response": {"id": "r2"}}),
            _sse({"type": "response.completed", "response": {"id": "r2"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    server_client = _LabelPatchRecordingServerClient([])

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    _session_histories_ref.pop(conv, None)

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": conv, "agent_id": "ag_adv"},
            )
            assert resp.status_code == 201
            for text in ("first", "second"):
                resp = await client.post(
                    f"/v1/sessions/{conv}/events?stream=true",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_adv",
                        "model": "test",
                        "content": [{"type": "input_text", "text": text}],
                    },
                )
                assert resp.status_code == 200
                assert "response.completed" in resp.text

        assert len(hc.posted_bodies) == 2
        assert hc.posted_bodies[0].get("model_override") == "model-pricey"
        assert hc.posted_bodies[1].get("model_override") == "model-pricey"
        assert len(_advisor_note_items(hc.posted_bodies[0].get("content") or [])) == 1
        assert _advisor_note_items(hc.posted_bodies[1].get("content") or []) == []
    finally:
        _session_histories_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_advisor_spec_for_session_swaps_sub_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stream advisor planning resolves against the child sub-agent spec."""
    from tests.runner.test_app_sessions_native import _patch_judge_returns_pricey

    conv = "conv_adv_child"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    parent = _polly_spec_tree()
    _patch_judge_returns_pricey(monkeypatch)

    async def _resolver(agent_id: str, session_id: str | None = None) -> object:
        del agent_id, session_id
        from omnigent.runner.app import ResolvedSpec

        return ResolvedSpec(spec=parent, workdir=bundle)

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r_child"}}),
            _sse({"type": "response.completed", "response": {"id": "r_child"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create = await client.post(
            "/v1/sessions",
            json={
                "session_id": conv,
                "agent_id": PARENT_AGENT_ID,
                "sub_agent_name": SUB_AGENT_NAME,
            },
        )
        assert create.status_code == 201
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": PARENT_AGENT_ID,
                "model": "base",
                "harness": "claude-native",
                "content": [{"type": "input_text", "text": "plan"}],
            },
        )
        assert resp.status_code == 200
        _ = resp.text

    assert pm.get_client_calls
    assert pm.get_client_calls[0][1] == "claude-native"


@pytest.mark.asyncio
async def test_run_turn_bg_empty_history_keeps_raw_inbound_content() -> None:
    """Fire-and-forget turns without history pass inbound content through unchanged."""
    conv = f"conv_raw_inbound_{uuid.uuid4().hex[:8]}"
    inbound = [{"type": "input_text", "text": "RAW_ONLY"}]
    spec = AgentSpec(
        spec_version=1,
        name="raw-inbound",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_raw"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_raw"}}),
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
                    "agent_id": "ag_raw",
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
async def test_terminal_launch_failure_closes_started_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed bridge-inject terminal launches tear down a relay they started."""
    import omnigent.claude_native_bridge as _bridge_mod

    relay_closed: list[bool] = []

    class _FakeRelay:
        def close(self) -> None:
            relay_closed.append(True)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", lambda **_k: _FakeRelay())

    class _FailingRegistry(_StubResourceRegistry):
        async def launch_required_terminal(self, *_a: object, **_k: object) -> object:
            raise RuntimeError("tmux spawn failed")

    session_id = f"conv_launch_fail_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    try:
        app = create_runner_app(
            resource_registry=_FailingRegistry(tmp_path),
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert resp.status_code == 500
        assert relay_closed == [True]
        assert not (_TOOL_RELAY_FILE and (bridge_dir / _TOOL_RELAY_FILE).exists())
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_recreate_repl_terminal_returns_none_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dead REPL attach degrades to 4404 when recreation fails."""
    conv = f"conv_repl_fail_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("tui", "main", tmp_path, running=False)
    terminal_registry._by_conversation.setdefault(conv, {})[("tui", "main")] = instance
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    resource_registry._terminal_roles[(conv, "terminal_tui_main")] = OMNIGENT_REPL_TERMINAL_ROLE

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("repl recreate failed")

    monkeypatch.setattr(runner_app_mod, "_auto_create_repl_terminal", _boom)

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
async def test_filesystem_diff_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Diff endpoint surfaces InvalidPath messages verbatim."""
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
    registry._primary_envs["conv_diff_trav"] = os_env

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(
            f"/v1/sessions/conv_diff_trav/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/diff/" + "..%2F..%2Fsecret.txt"
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_filesystem_write_seeds_snapshot_before_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Filesystem writes seed diff snapshots for pre-existing files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "note.txt"
    target.write_text("before-content", encoding="utf-8")
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(ws),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    registry = SessionResourceRegistry()
    registry._primary_envs["conv_seed"] = os_env
    seeded: list[tuple[str, str]] = []

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    def _capture_seed(path: str, content: str, *, session_id: str) -> None:
        seeded.append((path, content))

    monkeypatch.setattr(app.state.filesystem_registry, "seed_snapshot", _capture_seed)

    async with _runner_client(app) as client:
        resp = await client.put(
            f"/v1/sessions/conv_seed/resources/environments/"
            f"{DEFAULT_ENVIRONMENT_ID}/filesystem/note.txt",
            json={"content": "after-content"},
        )

    assert resp.status_code == 200
    assert seeded == [("note.txt", "before-content")]


@pytest.mark.asyncio
async def test_session_skills_use_cwd_when_no_workspace(tmp_path: Path) -> None:
    """Skill discovery falls back to cwd when no workspace or bundle root exists."""
    conv = f"conv_skills_cwd_{uuid.uuid4().hex[:8]}"

    class _NoWorkspaceServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if url.endswith(f"/sessions/{conv}"):
                return _json_response({"agent_id": "ag_skills"})
            return await super().get(url, **kwargs)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="skills-only")

    app = create_runner_app(
        spec_resolver=_resolver,
        server_client=_NoWorkspaceServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.get(f"/v1/sessions/{conv}/skills")

    assert resp.status_code == 200
    assert "skills" in resp.json()


@pytest.mark.asyncio
async def test_skill_resolve_rejects_non_object_body() -> None:
    """POST /skills/resolve rejects JSON arrays."""
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
            "/v1/sessions/conv_skill_array/skills/resolve",
            content=json.dumps(["not", "an", "object"]),
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_mcp_execute_returns_error_when_spec_resolver_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner /mcp/execute tolerates spec_resolver failures for cold sessions."""
    conv = f"conv_mcp_spec_{uuid.uuid4().hex[:8]}"

    async def _boom(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        raise RuntimeError("resolver down")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_boom,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=_FakeMcpManager(),
    )
    _session_agent_ids_ref[conv] = "ag_mcp"

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={"method": "tools/list", "params": {}},
        )

    assert resp.status_code == 200
    assert "No spec available" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_catch_up_scan_paginates_items_and_starts_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect catch-up scan follows pagination cursors and appends history."""
    conv = f"conv_catch_page_{uuid.uuid4().hex[:8]}"
    spec = AgentSpec(
        spec_version=1,
        name="catch-page",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    calls: list[str | None] = []
    prior_histories = dict(_session_histories_ref)

    class _PagedItemsServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            params = kwargs.get("params") or {}
            if url.endswith(f"/sessions/{conv}") and "/items" not in url:
                return _json_response({"agent_id": "ag_catch"})
            if url.endswith("/items"):
                calls.append(params.get("after"))
                if params.get("after") is None:
                    return _json_response(
                        {
                            "data": [
                                {
                                    "id": "item_1",
                                    "type": "message",
                                    "role": "user",
                                    "content": [],
                                }
                            ],
                            "has_more": True,
                        }
                    )
                return _json_response(
                    {
                        "data": [
                            {
                                "id": "item_2",
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "new"}],
                            }
                        ],
                        "has_more": False,
                    }
                )
            return await super().get(url, **kwargs)

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_cu"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_cu"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PagedItemsServer(),  # type: ignore[arg-type]
    )

    _session_histories_ref.clear()
    _session_histories_ref[conv] = [{"type": "message", "role": "user", "content": []}]
    _session_agent_ids_ref[conv] = "ag_catch"
    try:
        await app.state.catch_up_scan()
        for _ in range(200):
            if pm.get_client_calls:
                break
            await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.clear()
        _session_histories_ref.update(prior_histories)
        _session_agent_ids_ref.pop(conv, None)

    assert calls == [None, "item_1"]
    assert pm.get_client_calls


@pytest.mark.asyncio
async def test_catch_up_scan_stops_on_non_200_items_response() -> None:
    """Catch-up scan stops paging when the items endpoint returns non-200."""
    conv = f"conv_catch_404_{uuid.uuid4().hex[:8]}"
    _session_histories_ref[conv] = [{"type": "message", "role": "user", "content": []}]

    class _Items404Server(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if url.endswith("/items"):
                class _Resp:
                    status_code = 404

                    def json(self) -> dict[str, Any]:
                        return {}

                return _Resp()
            return await super().get(url, **kwargs)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=_Items404Server(),  # type: ignore[arg-type]
    )

    try:
        await app.state.catch_up_scan()
    finally:
        _session_histories_ref.pop(conv, None)


def test_build_spawn_env_import_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn-env builder returns None when harness env imports are unavailable."""
    spec = AgentSpec(
        spec_version=1,
        name="claude",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    def _boom(*_a: object, **_k: object) -> dict[str, str]:
        raise ImportError("missing sdk")

    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_claude_sdk_spawn_env",
        _boom,
    )
    assert _build_spawn_env_from_spec(spec, "claude-sdk") is None


def test_native_terminal_start_error_payload_shape() -> None:
    """Sanity-check the native terminal error payload helper remains structured."""
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Claude")
    assert isinstance(payload["code"], str)
    assert isinstance(payload["message"], str)
