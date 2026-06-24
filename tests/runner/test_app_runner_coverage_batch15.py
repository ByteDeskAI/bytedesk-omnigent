"""Batch-15 coverage for the last runner.app gaps."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.runner.app import _session_histories_ref, _session_inboxes_ref
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_mcp_summarize_edges import _FakeMcpManager
from tests.runner.test_app_runner_route_edges import _PaginatedServerClient, _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _LabelPatchRecordingServerClient,
    _ScriptedHarnessClient,
    _WakeRecordingServerClient,
    _advisor_orchestrator_spec,
    _runner_client,
)
from tests.runner.test_comment_relay import _StubResourceRegistry, _TOOL_RELAY_FILE
from tests.runner.test_native_subagent_harness_resolution import (
    PARENT_AGENT_ID,
    SUB_AGENT_NAME,
    _polly_spec_tree,
)


@pytest.mark.asyncio
async def test_concurrent_session_registration_reuses_snapshot_under_lock(
    tmp_path: Path,
) -> None:
    """Followers waiting on the snapshot lock return the cached snapshot."""
    conv = f"conv_snap_lock_{uuid.uuid4().hex[:8]}"
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

    spec = AgentSpec(
        spec_version=1,
        name="snap-lock",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_resolver,
        server_client=server_client,
        resource_registry=_StubResourceRegistry(tmp_path),
    )
    changes_url = (
        f"/v1/sessions/{conv}/resources/environments/"
        f"{DEFAULT_ENVIRONMENT_ID}/changes"
    )
    try:
        async with _runner_client(app) as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_snap"})
            first = asyncio.create_task(client.get(changes_url))
            await asyncio.wait_for(snapshot_started.wait(), timeout=5.0)
            followers = [asyncio.create_task(client.get(changes_url)) for _ in range(3)]
            release.set()
            responses = await asyncio.gather(first, *followers)
    finally:
        await server_client.aclose()

    assert all(r.status_code == 200 for r in responses)
    assert snapshot_count == 1


@pytest.mark.asyncio
async def test_delete_session_sets_cancel_on_async_tool_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /v1/sessions signals cancel on in-flight sys_call_async tasks."""
    conv = f"conv_del_async_{uuid.uuid4().hex[:8]}"
    cancel_observed = asyncio.Event()
    slow_started = asyncio.Event()

    async def _slow_os_env_tool(*_args: Any, **_kwargs: Any) -> str:
        slow_started.set()
        await asyncio.sleep(5.0)
        return "done"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _slow_os_env_tool)

    spec = AgentSpec(
        spec_version=1,
        name="async-del",
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

    async def _watch_inbox(inbox: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            item = await inbox.get()
            if isinstance(item, dict) and item.get("status") == "cancelled":
                cancel_observed.set()
                return

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_async"},
                )
            ).status_code == 201
            inbox = _session_inboxes_ref[conv]
            watcher = asyncio.create_task(_watch_inbox(inbox))
            dispatch = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {
                        "name": "sys_call_async",
                        "arguments": {
                            "tool": "sys_os_read",
                            "args": json.dumps({"path": "/etc/hosts"}),
                        },
                    },
                },
            )
            assert dispatch.status_code == 200, dispatch.text
            await asyncio.wait_for(slow_started.wait(), timeout=5.0)
            await client.delete(f"/v1/sessions/{conv}")
            await asyncio.wait_for(cancel_observed.wait(), timeout=5.0)
            watcher.cancel()
    finally:
        _session_inboxes_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_on_proxy_stream_end_without_event_loop_skips_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn end tolerates missing event loop when scheduling continuations."""
    conv = f"conv_no_loop_{uuid.uuid4().hex[:8]}"
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_nl"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_nl"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    calls = 0

    def _boom_loop() -> asyncio.AbstractEventLoop:
        nonlocal calls
        calls += 1
        if calls > 0:
            raise RuntimeError("no running loop")
        return orig_loop()

    orig_loop = asyncio.get_running_loop
    monkeypatch.setattr(asyncio, "get_running_loop", _boom_loop)

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_nl",
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
    assert hc.posted_bodies


class _PerTurnFwdBlockingHarness(_ScriptedHarnessClient):
    """Blocks each harness stream and each interrupt forward independently."""

    def __init__(
        self,
        sse_frames: list[str],
        *,
        fwd_gate: asyncio.Event,
    ) -> None:
        super().__init__(sse_frames)
        self._fwd_gate = fwd_gate
        self._turn_gate = asyncio.Event()
        self.post_seen = asyncio.Event()
        self.interrupt_forward_started = asyncio.Event()

    def arm_turn_block(self) -> None:
        """Block the next harness stream at its second frame."""
        self._turn_gate.clear()

    def release_turn_block(self) -> None:
        """Release the currently blocked harness stream."""
        self._turn_gate.set()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt":
            self.interrupt_forward_started.set()
            await self._fwd_gate.wait()
        return await super().post(url, json=json, timeout=timeout)

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        self.post_seen.set()
        frames = self._sse_frames
        turn_gate = self._turn_gate

        class _BlockingCtx:
            status_code = 200

            async def __aenter__(self) -> _PerTurnFwdBlockingHarness._BlockingHandle:
                return _PerTurnFwdBlockingHarness._BlockingHandle(frames, turn_gate)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _BlockingCtx()

    class _BlockingHandle:
        status_code = 200

        def __init__(self, frames: list[str], turn_gate: asyncio.Event) -> None:
            self._frames = frames
            self._turn_gate = turn_gate

        async def aiter_text(self) -> Any:
            for i, frame in enumerate(self._frames):
                if i == 1:
                    await self._turn_gate.wait()
                yield frame


@pytest.mark.asyncio
async def test_interrupt_with_stale_expected_task_is_noop_cancel() -> None:
    """Interrupt does not cancel a replacement turn when the original finished."""
    conv = f"conv_stale_cancel_{uuid.uuid4().hex[:8]}"
    fwd_gate = asyncio.Event()
    hc = _PerTurnFwdBlockingHarness(
        [
            _sse({"type": "response.created", "response": {"id": "resp_a"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_a"}}),
        ],
        fwd_gate=fwd_gate,
    )
    hc.arm_turn_block()
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="stale-cancel")

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        first = await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_stale",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "one"}],
            },
        )
        assert first.status_code == 202
        await asyncio.wait_for(hc.post_seen.wait(), timeout=5.0)
        hc.post_seen.clear()

        int_task = asyncio.create_task(
            client.post(f"/v1/sessions/{conv}/events", json={"type": "interrupt"})
        )
        await asyncio.wait_for(hc.interrupt_forward_started.wait(), timeout=5.0)

        hc.release_turn_block()
        await asyncio.sleep(0.1)
        hc.arm_turn_block()
        hc.post_seen.clear()

        second = await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_stale",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "two"}],
            },
        )
        assert second.status_code == 202
        await asyncio.wait_for(hc.post_seen.wait(), timeout=5.0)

        fwd_gate.set()
        interrupt = await int_task
        assert interrupt.status_code == 204
        assert len(hc.posted_bodies) == 2
        hc.release_turn_block()


@pytest.mark.asyncio
async def test_subagent_wake_skips_when_parent_inbox_missing() -> None:
    """Sub-agent completion does not wake a parent with no inbox queue."""
    parent_id = f"conv_parent_no_inbox_{uuid.uuid4().hex[:8]}"
    child_id = f"conv_child_no_inbox_{uuid.uuid4().hex[:8]}"
    server_client = _WakeRecordingServerClient(parent_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app_mod.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="no-inbox",
    )
    _session_inboxes_ref.pop(parent_id, None)

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE"},
                },
            )
            assert resp.status_code == 503, resp.text
            assert resp.json()["reason"] == "missing_parent_inbox"
            await asyncio.sleep(0.05)
        assert server_client.wake_posts == []
    finally:
        runner_app_mod.unregister_subagent_work(child_id)


@pytest.mark.asyncio
async def test_rewake_parent_without_work_entries_returns_quietly() -> None:
    """Idle recovery clears a stuck wake flag even when work entries are gone."""
    parent_id = f"conv_parent_no_entries_{uuid.uuid4().hex[:8]}"
    child_id = f"conv_child_no_entries_{uuid.uuid4().hex[:8]}"
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
    _session_inboxes_ref[parent_id] = inbox

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
        _session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_comment_relay_skips_when_concurrent_starter_publishes_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second relay starter returns when the first publishes during bridge-id resolve."""
    session_id = f"conv_relay_race_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    bridge_started = asyncio.Event()
    release_bridge = asyncio.Event()

    async def _slow_bridge_id(*_args: Any, **_kwargs: Any) -> str:
        bridge_started.set()
        await release_bridge.wait()
        return session_id

    monkeypatch.setattr(
        runner_app_mod,
        "_claude_native_bridge_id_for_session",
        _slow_bridge_id,
    )

    app = create_runner_app(
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    launch_body = {"terminal": "claude", "session_key": "main", "bridge_inject_dir": True}
    terminal_url = f"/v1/sessions/{session_id}/resources/terminals"

    try:
        async with _runner_client(app) as client:
            first = asyncio.create_task(client.post(terminal_url, json=launch_body))
            await asyncio.wait_for(bridge_started.wait(), timeout=5.0)
            second = asyncio.create_task(client.post(terminal_url, json=launch_body))
            await asyncio.sleep(0.05)
            release_bridge.set()
            r1, r2 = await asyncio.gather(first, second)
        assert r1.status_code == 200
        assert r2.status_code == 200
        relay_file = bridge_dir / _TOOL_RELAY_FILE
        assert relay_file.exists()
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_comment_relay_spec_resolution_failure_uses_fallback_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay start falls back to read-only tools when no spec is available."""
    import omnigent.claude_native_bridge as _bridge_mod

    captured_tools: list[list[dict[str, Any]]] = []
    real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        captured_tools.append(kwargs["tools"])
        return real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)
    monkeypatch.setattr(_bridge_mod, "post_tools_changed", lambda *_a, **_k: None)

    session_id = f"conv_relay_spec_fail_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    app = create_runner_app(
        spec_resolver=None,
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
        assert resp.status_code == 200
        assert captured_tools
        names = {tool["name"] for tool in captured_tools[0]}
        assert "sys_session_list" in names
        assert "sys_session_send" not in names
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_comment_relay_aborts_when_relay_appears_during_spec_resolve(
    tmp_path: Path,
) -> None:
    """Relay start aborts when a concurrent starter publishes during spec resolve."""
    session_id = f"conv_relay_spec_race_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    spec_started = asyncio.Event()
    release_spec = asyncio.Event()
    parent = _polly_spec_tree()

    async def _slow_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        spec_started.set()
        await release_spec.wait()
        return parent

    class _SnapshotServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if f"/sessions/{session_id}" in url:
                return httpx.Response(
                    200,
                    json={"id": session_id, "agent_id": "ag_relay", "labels": {}},
                    request=httpx.Request("GET", url),
                )
            return await super().get(url, **kwargs)

    app = create_runner_app(
        spec_resolver=_slow_resolver,
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=_SnapshotServer(),  # type: ignore[arg-type]
    )
    launch_body = {"terminal": "claude", "session_key": "main", "bridge_inject_dir": True}
    terminal_url = f"/v1/sessions/{session_id}/resources/terminals"

    try:
        async with _runner_client(app) as client:
            first = asyncio.create_task(client.post(terminal_url, json=launch_body))
            await asyncio.wait_for(spec_started.wait(), timeout=5.0)
            second = asyncio.create_task(client.post(terminal_url, json=launch_body))
            await asyncio.sleep(0.02)
            release_spec.set()
            r1, r2 = await asyncio.gather(first, second)
        assert r1.status_code == 200
        assert r2.status_code == 200
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_tools_changed_notification_runtime_error_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cold-path relay notify tolerates RuntimeError from post_tools_changed."""
    import logging

    session_id = f"conv_tools_changed_err_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    def _raise_runtime(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("bridge server not ready")

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.post_tools_changed",
        _raise_runtime,
    )

    app = create_runner_app(
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
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
            await asyncio.sleep(0.1)
        assert any(
            "tools-changed notification skipped" in r.message for r in caplog.records
        )
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_stream_advisor_resolves_sub_agent_spec_for_child_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream advisor planning uses the child sub-agent spec, not the parent."""
    from omnigent.cost_plan import AdvisorVerdict
    from omnigent.runner import cost_advisor as cost_advisor_mod

    conv = f"conv_adv_sub_{uuid.uuid4().hex[:8]}"
    parent = _advisor_orchestrator_spec()
    judged: list[str] = []

    class _RecordingJudge:
        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
            del query, turn_anchor
            judged.append("called")
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
            _sse({"type": "response.created", "response": {"id": "r_sub"}}),
            _sse({"type": "response.completed", "response": {"id": "r_sub"}}),
        ]
    )
    server_client = _LabelPatchRecordingServerClient([])

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return parent

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    try:
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
                    "harness": "claude-sdk",
                    "content": [{"type": "input_text", "text": "plan"}],
                },
            )
            assert resp.status_code == 200
            _ = resp.text
        assert judged == ["called"]
    finally:
        _session_histories_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_run_turn_bg_loads_history_when_cache_cleared_mid_turn() -> None:
    """Background turns reload server history when the in-memory cache was cleared."""
    conv = f"conv_reload_hist_{uuid.uuid4().hex[:8]}"
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "FROM_SERVER"}],
        }
    ]
    spec = AgentSpec(
        spec_version=1,
        name="reload-hist",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_rh"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_rh"}}),
        ]
    )
    setup_gate = asyncio.Event()
    setup_entered = asyncio.Event()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        setup_entered.set()
        await setup_gate.wait()
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PaginatedServerClient(history),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_rh",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "LOCAL"}],
                },
            )
            assert resp.status_code == 202
            await asyncio.wait_for(setup_entered.wait(), timeout=5.0)
            _session_histories_ref.pop(conv, None)
            setup_gate.set()
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)

    assert hc.posted_bodies
    content = hc.posted_bodies[0].get("content") or []
    assert any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and any(
            b.get("text") == "FROM_SERVER"
            for b in item.get("content", [])
            if isinstance(b, dict)
        )
        for item in content
    )


@pytest.mark.asyncio
async def test_run_turn_bg_uses_inbound_content_when_history_empty() -> None:
    """Background turns pass inbound content when the seeded history list is empty."""
    conv = f"conv_empty_hist_{uuid.uuid4().hex[:8]}"
    inbound = [{"type": "input_text", "text": "INBOUND_ONLY"}]
    spec = AgentSpec(
        spec_version=1,
        name="empty-hist",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_eh"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_eh"}}),
        ]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PaginatedServerClient([]),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_eh",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": inbound,
                },
            )
            assert resp.status_code == 202
            _session_histories_ref[conv] = []
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)

    assert hc.posted_bodies
    # Falsy history ([]) uses msg_body content directly — not the cold-cache wrap.
    assert hc.posted_bodies[0]["content"] == inbound


@pytest.mark.asyncio
async def test_mcp_elicitation_retry_without_owning_server_returns_error() -> None:
    """MRTR retry surfaces an error when no live MCP server owns the tool."""
    conv = f"conv_mcp_no_owning_{uuid.uuid4().hex[:8]}"
    spec = AgentSpec(spec_version=1, name="mcp-agent")

    class _NoOwningMcpManager(_FakeMcpManager):
        def _resolve_owning_server(self, spec: AgentSpec, bare_tool: str) -> Any:
            del spec, bare_tool
            return None

    mcp = _NoOwningMcpManager()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        mcp_manager=mcp,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_mcp"})
        resp = await client.post(
            f"/v1/sessions/{conv}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "srv__search",
                    "arguments": {"q": "z"},
                    "inputResponses": {"req-1": {"action": "accept"}},
                    "requestState": "state-xyz",
                },
            },
        )

    assert resp.status_code == 200
    assert "error" in resp.json()
    assert "No spec available" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_summarize_returns_none_connection_for_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summarize skips session auth lookup when the session spec was never cached."""
    from types import SimpleNamespace

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

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": "conv_never_seen",
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"] is None


@pytest.mark.asyncio
async def test_summarize_returns_none_when_cached_spec_entry_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summarize treats a cached null spec entry as missing credentials."""
    from types import SimpleNamespace

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

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_null_spec_cache"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
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