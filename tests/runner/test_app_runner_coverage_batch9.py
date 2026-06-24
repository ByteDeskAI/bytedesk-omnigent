"""Batch-9 coverage for session GET edges, history load failures, and native interrupts."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any
import httpx
import pytest

from omnigent import codex_native_bridge

from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_runner_route_edges import _PaginatedServerClient, _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)


class _HistoryStatusFailServer(NullServerClient):
    """Returns non-200 for paginated history GETs."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.endswith("/items"):
            return type(
                "_Resp",
                (),
                {"status_code": 503, "json": lambda self: {"error": "unavailable"}},
            )()
        return NullServerClient._Response()


class _HistoryTransportFailServer(NullServerClient):
    """Raises on paginated history GETs."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.endswith("/items"):
            raise httpx.ConnectError("history transport down")
        return NullServerClient._Response()


@pytest.mark.asyncio
async def test_get_session_returns_500_when_start_time_cache_missing() -> None:
    """GET /v1/sessions surfaces corrupt runner state when start_time is absent."""
    sid = "conv_no_start"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(sid)
    runner_app_mod._session_agent_ids_ref[sid] = "ag_1"

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.get(f"/v1/sessions/{sid}")
    finally:
        runner_app_mod._session_agent_ids_ref.pop(sid, None)
        pm._sessions.discard(sid)

    assert resp.status_code == 500
    assert "start_time missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_session_returns_503_when_spec_resolver_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """POST /v1/sessions maps resolver transport failures to spec_resolver_failed."""

    async def _boom(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        raise RuntimeError("resolver unavailable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_boom,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": "conv_resolver_fail", "agent_id": "ag_1"},
            )

    assert resp.status_code == 503
    assert resp.json()["error"] == "spec_resolver_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_client", "log_fragment"),
    [
        (_HistoryStatusFailServer(), "History load returned"),
        (_HistoryTransportFailServer(), "History load failed"),
    ],
)
async def test_history_load_failure_logs_and_continues_turn(
    server_client: NullServerClient,
    log_fragment: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """History pagination failures are logged and the turn still dispatches."""
    spec = AgentSpec(spec_version=1, name="history-fail")
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_hf"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_hf"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": "conv_hist_fail", "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    "/v1/sessions/conv_hist_fail/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "next"}],
                    },
                )
            ).status_code == 202
            for _ in range(200):
                if harness_client.posted_bodies:
                    break
                await asyncio.sleep(0.01)

    assert log_fragment in caplog.text
    assert harness_client.posted_bodies


@pytest.mark.asyncio
async def test_delete_session_unregisters_filesystem_conversation(tmp_path: Path) -> None:
    """DELETE /v1/sessions unregisters the session from the filesystem registry."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=tmp_path,
    )
    fs_registry = app.state.filesystem_registry
    assert fs_registry is not None
    unregistered: list[str] = []
    fs_registry.unregister_conversation = lambda sid: unregistered.append(sid)  # type: ignore[method-assign, assignment]

    async with _runner_client(app) as client:
        create = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_fs_delete", "agent_id": "ag_1"},
        )
        assert create.status_code == 201
        delete = await client.delete("/v1/sessions/conv_fs_delete")

    assert delete.status_code == 200
    assert unregistered == ["conv_fs_delete"]


@pytest.mark.asyncio
async def test_terminal_activity_publisher_emits_activity_event() -> None:
    """The runner wires a terminal-activity publisher onto the resource registry."""
    sid = "conv_term_activity"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    publisher = app.state.session_resource_registry._terminal_activity_publisher
    assert publisher is not None
    runner_app_mod._session_event_queues_ref[sid] = asyncio.Queue()

    try:
        publisher(sid, "terminal_bash_main")
        event = runner_app_mod._session_event_queues_ref[sid].get_nowait()
    finally:
        runner_app_mod._session_event_queues_ref.pop(sid, None)

    assert event == {
        "type": "session.terminal.activity",
        "session_id": sid,
        "terminal_id": "terminal_bash_main",
    }


@pytest.mark.asyncio
async def test_auxiliary_terminal_exit_does_not_fail_session(tmp_path: Path) -> None:
    """Auxiliary terminal exits publish deletion only, not session.status failed."""
    sid = f"conv_aux_exit_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("sidecar", "s1", tmp_path)
    instance.command = None
    terminal_registry._by_conversation.setdefault(sid, {})[("sidecar", "s1")] = instance
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
        await resource_registry.observe_auxiliary_terminal(sid, "sidecar", "s1", instance)
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

    assert any(e.get("type") == "session.resource.deleted" for e in events)
    assert not any(
        e.get("type") == "session.status" and e.get("status") == "failed" for e in events
    )


@pytest.mark.asyncio
async def test_required_terminal_exit_release_failure_is_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Required terminal exit still logs when harness release fails."""
    sid = f"conv_release_fail_{uuid.uuid4().hex[:8]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
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

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(sid)

    async def _boom_release(session_id: str) -> None:
        del session_id
        raise RuntimeError("release refused")

    pm.release = _boom_release  # type: ignore[method-assign]

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await resource_registry.observe_required_terminal(sid, "worker", "main", instance)
        on_exit = callbacks["on_exit"]
        assert callable(on_exit)
        on_exit()
        for _ in range(200):
            if "Failed to release harness subprocess after required terminal exit" in caplog.text:
                break
            await asyncio.sleep(0.01)

    assert "Failed to release harness subprocess after required terminal exit" in caplog.text


@pytest.mark.asyncio
async def test_codex_native_interrupt_skips_when_bridge_state_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex-native interrupt is a no-op when bridge state is absent."""
    conv_id = "conv_codex_no_bridge"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", Path("/tmp/codex-missing-bridge"))

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

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "ag_1"},
            )
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
            )

    assert resp.status_code == 204
    assert "Codex-native interrupt skipped" in caplog.text
    assert "no bridge state" in caplog.text


@pytest.mark.asyncio
async def test_pi_native_interrupt_enqueue_failure_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi-native interrupt surfaces inbox write failures as 503 JSON."""
    import omnigent.pi_native_bridge as pi_native_bridge

    conv_id = "conv_pi_interrupt_fail"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", Path("/tmp/pi-int-fail"))

    def _boom(_bridge_dir: Path) -> None:
        raise OSError("inbox not writable")

    monkeypatch.setattr(pi_native_bridge, "enqueue_interrupt", _boom)

    spec = AgentSpec(
        spec_version=1,
        name="pi",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
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
    assert resp.json()["error"] == "pi_native_interrupt_failed"


@pytest.mark.asyncio
async def test_codex_native_interrupt_skips_when_bridge_belongs_to_other_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex-native interrupt ignores stale bridge state for a different session."""
    conv_id = "conv_codex_wrong_bridge"
    other_id = "conv_codex_owner"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=other_id,
            socket_path="ws://127.0.0.1:9",
            thread_id="thread_other",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_other",
        ),
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

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "ag_1"},
            )
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
            )

    assert resp.status_code == 204
    assert "bridge belongs to" in caplog.text


@pytest.mark.asyncio
async def test_codex_native_interrupt_skips_when_no_active_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex-native interrupt is a no-op when the bridge has no active turn."""
    conv_id = "conv_codex_no_turn"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:9",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
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

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "ag_1"},
            )
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
            )

    assert resp.status_code == 204
    assert "no active turn" in caplog.text