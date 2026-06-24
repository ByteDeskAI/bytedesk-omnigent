"""Batch-16 coverage for the last runner.app statement gaps."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.errors import ErrorCode, OmnigentError

from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_runner_coverage_batch5 import _FailingGetServerClient
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _ADVISOR_TIERS_YAML,
    _FakeProcessManager,
    _LabelPatchRecordingServerClient,
    _ScriptedHarnessClient,
    _WakeRecordingServerClient,
    _runner_client,
)
from tests.runner.test_comment_relay import _StubResourceRegistry, _TOOL_RELAY_FILE
from tests.runner.test_native_subagent_harness_resolution import PARENT_AGENT_ID


@pytest.mark.asyncio
async def test_concurrent_snapshot_fetchers_recheck_cache_under_lock(
    tmp_path: Path,
) -> None:
    """Followers that waited on the snapshot lock return the cached snapshot."""
    conv = f"conv_snap_under_lock_{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fetch_count = 0
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            fetch_count += 1
            if fetch_count == 1:
                first_entered.set()
                await release.wait()
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": "ag_snap", "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="snap-lock")

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
            tasks = [
                asyncio.create_task(client.get(f"/v1/sessions/{conv}/skills"))
                for _ in range(6)
            ]
            await asyncio.wait_for(first_entered.wait(), timeout=5.0)
            await asyncio.sleep(0.05)
            release.set()
            responses = await asyncio.gather(*tasks)
    finally:
        await server_client.aclose()

    assert all(r.status_code == 200 for r in responses)
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_stream_disconnect_requeues_unsent_event() -> None:
    """A client disconnect during SSE delivery re-queues the unsent event."""
    sid = f"conv_stream_requeue_{uuid.uuid4().hex[:8]}"
    runner_app_mod._session_event_queues_ref.pop(sid, None)
    pending_event = {"type": "session.status", "status": "running", "id": "evt_1"}

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    stream_route = next(
        route
        for route in app.router.routes
        if getattr(route, "path", None) == "/v1/sessions/{session_id}/stream"
    )

    try:
        first = await stream_route.endpoint(session_id=sid)  # type: ignore[attr-defined]
        gen = first.body_iterator
        heartbeat = await gen.__anext__()
        assert b"session.heartbeat" in heartbeat

        queue = runner_app_mod._session_event_queues_ref[sid]
        queue.put_nowait(pending_event)
        consumer = asyncio.create_task(gen.__anext__())
        await gen.aclose()
        with pytest.raises((StopAsyncIteration, asyncio.CancelledError)):
            await consumer

        assert queue.qsize() == 1

        second = await stream_route.endpoint(session_id=sid)  # type: ignore[attr-defined]
        replay = second.body_iterator
        await replay.__anext__()
        frame = await replay.__anext__()
        assert pending_event["id"].encode() in frame
        queue.put_nowait(None)
        await replay.__anext__()
    finally:
        runner_app_mod._session_event_queues_ref.pop(sid, None)


def test_terminal_attach_repop_exits_when_no_tmux_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach-time repop is a no-op when no tmux client registers in time."""
    launched: list[str] = []
    monkeypatch.setattr(
        "omnigent.native_cost_popup.wait_for_tmux_client",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )

    async def _slow_bridge(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-no-client",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = f"conv_repop_no_client_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FailingGetServerClient(
            pending=[{"id": "el-1", "gate": "tool_call", "message": "approve?"}]
        ),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    client = TestClient(app)
    assert client.post(
        "/v1/sessions",
        json={"session_id": conv_id, "agent_id": "ag_repop"},
    ).status_code == 201

    with client.websocket_connect(
        f"/v1/sessions/{conv_id}/resources/terminals/terminal_codex_main/attach"
    ):
        pass

    assert launched == []


@pytest.mark.asyncio
async def test_comment_relay_continues_when_spec_resolve_raises_omnigent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay start falls back when session agent spec resolution raises."""
    session_id = f"conv_relay_spec_err_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    orig_unwrap = runner_app_mod._unwrap_resolved_spec

    def _unwrap_raises_inside_relay(entry: object) -> object:
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "_ensure_comment_relay_started":
                raise OmnigentError("agent not bound", code=ErrorCode.NOT_FOUND)
            frame = frame.f_back
        return orig_unwrap(entry)

    monkeypatch.setattr(runner_app_mod, "_unwrap_resolved_spec", _unwrap_raises_inside_relay)
    monkeypatch.setattr("omnigent.claude_native_bridge.post_tools_changed", lambda *_a, **_k: None)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="relay-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    class _SnapshotServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if f"/sessions/{session_id}" in url:
                return httpx.Response(
                    200,
                    json={"id": session_id, "agent_id": "ag_relay", "labels": {}},
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        spec_resolver=_resolver,
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=_SnapshotServer(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
        assert resp.status_code == 200
        relay_file = bridge_dir / _TOOL_RELAY_FILE
        assert relay_file.exists()
        names = {t["name"] for t in json.loads(relay_file.read_text())["tools"]}
        assert "sys_session_list" in names
        assert "sys_session_send" not in names
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_stream_advisor_resolves_polly_child_sub_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream advisor planning swaps to the named worker child spec."""
    from omnigent.cost_plan import AdvisorVerdict
    from omnigent.runner import cost_advisor as cost_advisor_mod

    conv = f"conv_adv_polly_child_{uuid.uuid4().hex[:8]}"
    parent = AgentSpec(
        spec_version=1,
        name="advisor-orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(
                    type="omnigent",
                    config={
                        "harness": "claude-sdk",
                        "cost_optimize": _ADVISOR_TIERS_YAML,
                    },
                ),
            ),
        ],
    )
    judged_models: list[str | None] = []

    class _RecordingJudge:
        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
            del query, turn_anchor
            judged_models.append("called")
            return AdvisorVerdict(
                tier="expensive",
                model="model-pricey",
                applied=False,
                rationale="child plan",
                turn_anchor="anchor",
            )

    monkeypatch.setattr(
        cost_advisor_mod,
        "build_llm_judge",
        lambda **_kwargs: _RecordingJudge(),
    )

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r_child"}}),
            _sse({"type": "response.completed", "response": {"id": "r_child"}}),
        ]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return parent

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabelPatchRecordingServerClient([]),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            create = await client.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": "worker",
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
                    "harness": "claude-sdk",
                    "content": [{"type": "input_text", "text": "plan"}],
                },
            )
            assert resp.status_code == 200
            _ = resp.text
        assert judged_models == ["called"]
    finally:
        runner_app_mod._session_histories_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_subagent_wake_skips_when_event_loop_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agent wake scheduling is a no-op when no event loop is available."""
    parent_id = f"conv_parent_no_loop_{uuid.uuid4().hex[:8]}"
    child_id = f"conv_child_no_loop_{uuid.uuid4().hex[:8]}"
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)

    orig_loop = asyncio.get_running_loop

    def _loop_or_raise_in_wake_scheduler() -> asyncio.AbstractEventLoop:
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "_schedule_subagent_wake":
                raise RuntimeError("no running loop")
            frame = frame.f_back
        return orig_loop()

    monkeypatch.setattr(asyncio, "get_running_loop", _loop_or_raise_in_wake_scheduler)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app_mod._session_inboxes_ref[parent_id] = inbox
    runner_app_mod.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="no-loop",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE"},
                },
            )
            assert resp.status_code == 204, resp.text
            await asyncio.sleep(0.05)
        assert server_client.wake_posts == []
    finally:
        runner_app_mod._session_inboxes_ref.pop(parent_id, None)
        runner_app_mod.unregister_subagent_work(child_id)


@pytest.mark.asyncio
async def test_rewake_parent_clears_flag_without_work_entries() -> None:
    """Idle recovery clears a stuck wake flag even when work entries are gone."""
    parent_id = f"conv_parent_no_entries_{uuid.uuid4().hex[:8]}"
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbox.put_nowait({"handle_id": "orphan", "output": "stale"})
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_ne"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_ne"}}),
        ],
        stream_finished=gate,
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app_mod._session_inboxes_ref[parent_id] = inbox

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_parent",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake"}],
                },
            )
            assert resp.status_code == 202
            await asyncio.wait_for(gate.wait(), timeout=5.0)
            await asyncio.sleep(0.05)
        assert len(server_client.wake_posts) <= 1
    finally:
        runner_app_mod._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_summarize_returns_none_when_cached_resolved_spec_has_null_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Summarize treats a cached ResolvedSpec with spec=None as missing auth."""
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="ok")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )

    phase = {"cache_null": False}

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec | ResolvedSpec:
        del agent_id, session_id
        if phase["cache_null"]:
            return ResolvedSpec(spec=None, workdir=tmp_path)
        return AgentSpec(spec_version=1, name="summarize-agent")

    conv = f"conv_null_resolved_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        phase["cache_null"] = True
        await client.get(f"/v1/sessions/{conv}/skills")
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