"""Batch-10 coverage for stream lazy queue, delete cleanup, and env factory."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnigent import codex_native_app_server
from omnigent import codex_native_bridge
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import create_runner_app_from_env, register_timer
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _BlockingHarnessClient,
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)


@pytest.mark.asyncio
async def test_stream_lazily_creates_event_queue_before_session_init() -> None:
    """GET /stream creates the per-session queue when init has not run yet."""
    original = runner_app_mod._SESSION_STREAM_HEARTBEAT_S
    runner_app_mod._SESSION_STREAM_HEARTBEAT_S = 0.05
    sid = f"conv_lazy_stream_{uuid.uuid4().hex[:8]}"
    runner_app_mod._session_event_queues_ref.pop(sid, None)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:

            async def _subscribe() -> None:
                async with client.stream("GET", f"/v1/sessions/{sid}/stream") as stream:
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        event = json.loads(payload)
                        if event.get("type") == "session.heartbeat":
                            return

            sub_task = asyncio.create_task(_subscribe())
            for _ in range(100):
                if sid in runner_app_mod._session_event_queues_ref:
                    break
                await asyncio.sleep(0.02)
            assert sid in runner_app_mod._session_event_queues_ref

            queue = runner_app_mod._session_event_queues_ref[sid]
            queue.put_nowait(None)
            await asyncio.wait_for(sub_task, timeout=2.0)
    finally:
        runner_app_mod._SESSION_STREAM_HEARTBEAT_S = original
        runner_app_mod._session_event_queues_ref.pop(sid, None)


@pytest.mark.asyncio
async def test_delete_session_cancels_registered_timers() -> None:
    """DELETE /v1/sessions cancels module-level timer tasks for the session."""
    sid = f"conv_del_timer_{uuid.uuid4().hex[:8]}"

    class _FakeTimerTask:
        def __init__(self) -> None:
            self.cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            self.cancelled = True
            return True

    timer_task = _FakeTimerTask()
    register_timer(sid, "timer_del", timer_task)  # type: ignore[arg-type]

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": sid, "agent_id": "ag_1"},
                )
            ).status_code == 201
            resp = await client.delete(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert timer_task.cancelled is True
    finally:
        runner_app_mod._session_timers.pop(sid, None)


@pytest.mark.asyncio
async def test_required_terminal_exit_unknown_command_in_failure_message(
    tmp_path: Path,
) -> None:
    """Required terminal exits without launch metadata report command as unknown."""
    sid = f"conv_req_unknown_cmd_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    instance.command = None
    terminal_registry._by_conversation.setdefault(sid, {})[("worker", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    runner_app_mod._session_event_queues_ref[sid] = asyncio.Queue()

    try:
        await resource_registry.observe_required_terminal(sid, "worker", "main", instance)
        on_exit = callbacks["on_exit"]
        assert callable(on_exit)
        on_exit()
        await asyncio.sleep(0.05)
        events: list[dict[str, Any]] = []
        queue = runner_app_mod._session_event_queues_ref[sid]
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                events.append(item)
    finally:
        runner_app_mod._session_event_queues_ref.pop(sid, None)

    failed = [
        e
        for e in events
        if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert failed, f"expected session.status failed, got {events!r}"
    message = str(failed[0].get("error", {}).get("message", ""))
    assert "unknown" in message.lower()


@pytest.mark.asyncio
async def test_codex_native_interrupt_rpc_failure_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex-native interrupt surfaces app-server RPC failures as 503 JSON."""
    conv_id = "conv_codex_int_rpc_fail"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex",
        ),
    )

    class _FailingCodexClient:
        def __init__(self, transport: str, client_name: str) -> None:
            self.transport = transport
            self.client_name = client_name

        async def connect(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del method, params
            raise RuntimeError("turn/interrupt rejected")

        async def close(self) -> None:
            return None

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _FailingCodexClient:
        return _FailingCodexClient(transport, client_name)

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "codex_native_interrupt_failed"


def test_create_runner_app_from_env_requires_runner_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport subprocess factory fails loud when RUNNER_SERVER_URL is unset."""
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="RUNNER_SERVER_URL is required"):
        create_runner_app_from_env()


def test_create_runner_app_from_env_builds_fastapi_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured RUNNER_SERVER_URL yields a runner app backed by httpx."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")

    app = create_runner_app_from_env()

    assert app.state.session_resource_registry is not None


class _InterruptFailHarness(_BlockingHarnessClient):
    """Harness whose interrupt forward raises so the runner logs and continues."""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt":
            raise RuntimeError("harness interrupt transport down")
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_interrupt_logs_when_harness_forward_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In-process interrupt still cancels the turn when harness forward fails."""
    gate = asyncio.Event()
    spec = AgentSpec(spec_version=1, name="interrupt-fail")
    harness_client = _InterruptFailHarness(
        [
            _sse({"type": "response.created", "response": {"id": "resp_if"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_if"}}),
        ],
        gate,
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv_id = "conv_interrupt_fwd_fail"

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            turn_resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "go"}],
                    "harness": "openai-agents",
                },
            )
            assert turn_resp.status_code == 202
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            int_resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
            )

    assert int_resp.status_code == 204
    assert "Interrupt forward to harness failed" in caplog.text