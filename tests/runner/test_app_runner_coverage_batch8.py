"""Batch-8 coverage for native create_session bootstrap and session route edges."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent import claude_native_bridge
from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY, prepare_bridge_dir
from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)


class _NativeCreateServerClient(NullServerClient):
    """Server stub for native harness ``POST /v1/sessions`` bootstrap."""

    def __init__(self, *, session_labels: dict[str, str] | None = None) -> None:
        self._session_labels = session_labels or {}

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs

        class _Resp(NullServerClient._Response):
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def json(self) -> dict[str, Any]:
                return self._payload

        if url.endswith("/items"):
            return _Resp({"data": [], "has_more": False})
        if self._session_labels and "/sessions/" in url:
            return _Resp({"labels": self._session_labels, "agent_id": "ag_1"})
        return _Resp({"id": "conv", "agent_id": "ag_1"})


class _AlwaysFailWakeServerClient(NullServerClient):
    """Returns 503 for parent wake POSTs so delivery exhausts retries."""

    def __init__(self, parent_id: str) -> None:
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url == self._parent_events_path:
            request = httpx.Request("POST", f"http://test{url}")
            raise httpx.HTTPStatusError(
                "wake rejected",
                request=request,
                response=httpx.Response(503, request=request, json={"error": "down"}),
            )
        return await super().post(url, **kwargs)


def _native_spec(harness: str) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name=f"{harness}-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["grok-native", "pi-native"])
async def test_create_session_auto_creates_native_terminal(
    harness: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-spawned native sessions bootstrap their terminal on create."""
    terminal_name = harness.removesuffix("-native")
    auto_attr = f"_auto_create_{terminal_name}_terminal"
    created: list[str] = []

    async def _stub_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event, kwargs
        created.append(session_id)
        return SessionResourceView(
            id=terminal_resource_id(terminal_name, "main"),
            type="terminal",
            session_id=session_id,
            name=f"auto-{terminal_name}",
        )

    monkeypatch.setattr(runner_app_mod, auto_attr, _stub_create)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec(harness)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    sid = f"conv_{terminal_name}_bootstrap"
    async with _runner_client(app) as client:
        resp = await client.post("/v1/sessions", json={"session_id": sid, "agent_id": "ag_1"})

    assert resp.status_code == 201, resp.text
    assert created == [sid]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "auto_attr", "terminal_name"),
    [
        ("grok-native", "_auto_create_grok_terminal", "Grok"),
        ("pi-native", "_auto_create_pi_terminal", "Pi"),
    ],
)
async def test_create_session_native_terminal_failure_publishes_start_error(
    harness: str,
    auto_attr: str,
    terminal_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native terminal bootstrap failures publish session.status failed."""
    sid = f"conv_{terminal_name.lower()}_boot_fail"

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"{terminal_name} launch failed")

    monkeypatch.setattr(runner_app_mod, auto_attr, _boom)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec(harness)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NativeCreateServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    runner_app_mod._session_event_queues_ref[sid] = queue

    try:
        async with _runner_client(app) as client:
            resp = await client.post("/v1/sessions", json={"session_id": sid, "agent_id": "ag_1"})
        assert resp.status_code == 201

        events: list[dict[str, Any]] = []
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
    assert failed, f"expected session.status failed event, got {events!r}"
    assert terminal_name in str(failed[0].get("error", {}).get("message", ""))


@pytest.mark.asyncio
async def test_create_session_claude_rebuild_tears_down_stale_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending claude-native rebuild drops the stale terminal before auto-create."""
    session_id = "conv_claude_rebuild"
    bridge_id = "bridge_rebuild"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    prepare_bridge_dir(session_id, bridge_id=bridge_id, workspace=tmp_path)

    terminal_registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "claude.sock",
        private_dir=tmp_path / "claude",
        running=True,
    )
    terminal_registry._by_conversation[session_id] = {("claude", "main"): instance}

    cleaned: list[str] = []
    terminal_registry.cleanup_conversation = AsyncMock(side_effect=lambda sid: cleaned.append(sid))  # type: ignore[method-assign]

    created: list[str] = []

    async def _stub_create(
        sid: str,
        resource_registry: object,
        publish_event: object,
        **kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event, kwargs
        created.append(sid)
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=sid,
            name="auto-claude",
        )

    monkeypatch.setattr(runner_app_mod, "_auto_create_claude_terminal", _stub_create)

    server_client = _NativeCreateServerClient(
        session_labels={
            BRIDGE_ID_LABEL_KEY: bridge_id,
            FORK_CARRY_HISTORY_LABEL_KEY: "1",
        },
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("claude-native")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "ag_1"},
        )

    assert resp.status_code == 201, resp.text
    assert cleaned == [session_id]
    assert created == [session_id]


@pytest.mark.asyncio
async def test_create_session_repl_terminal_failure_does_not_fail_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REPL terminal launch failures are logged but the session still starts."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("repl launch failed")

    monkeypatch.setattr(runner_app_mod, "_auto_create_repl_terminal", _boom)

    spec = AgentSpec(
        spec_version=1,
        name="sdk-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": "conv_repl_fail", "agent_id": "ag_1"},
            )

    assert resp.status_code == 201
    assert "Failed to auto-create omnigent REPL terminal" in caplog.text


@pytest.mark.asyncio
async def test_get_session_returns_500_when_agent_id_cache_missing() -> None:
    """GET /v1/sessions surfaces corrupt runner state when agent_id is absent."""
    spec = AgentSpec(spec_version=1, name="t")
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": "conv_no_agent", "agent_id": "ag_1"})
        runner_app_mod._session_agent_ids_ref.pop("conv_no_agent", None)
        resp = await client.get("/v1/sessions/conv_no_agent")

    assert resp.status_code == 500
    assert "agent_id missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_subagent_turn_continues_when_snapshot_has_no_sub_agent_name() -> None:
    """Sub-agent recovery returns None when the snapshot omits sub_agent_name."""
    child_id = "conv_child_snap_none"
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_sf"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_sf"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

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
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_parent",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        _ = resp.text

    assert pm.get_client_calls, "turn should still reach the harness"


@pytest.mark.asyncio
async def test_subagent_wake_post_failure_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exhausted wake retries log a warning and release the debounce flag."""
    parent_id = "conv_parent_wake_fail"
    child_id = "conv_child_wake_fail"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _AlwaysFailWakeServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _record_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner_app_mod, "_wake_retry_sleep", _record_sleep)
    monkeypatch.setattr(runner_app_mod, "_WAKE_POST_MAX_ATTEMPTS", 1)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app_mod._session_inboxes_ref[parent_id] = session_inbox
    runner_app_mod.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="wake-fail",
    )

    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            async with _runner_client(app) as client:
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {"status": "idle", "output": "DONE"},
                    },
                )
                assert resp.status_code == 204, resp.text
                for _ in range(200):
                    if "Sub-agent wake POST failed" in caplog.text:
                        break
                    await asyncio.sleep(0.01)
    finally:
        runner_app_mod.unregister_subagent_work(child_id)
        runner_app_mod._session_inboxes_ref.pop(parent_id, None)

    assert session_inbox.qsize() == 1
    assert "Sub-agent wake POST failed" in caplog.text


@pytest.mark.asyncio
async def test_codex_terminal_ensure_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex ensure failures return native_terminal_start_failed JSON."""
    sid = "conv_codex_ensure_fail"

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("codex ensure boom")

    async def _no_terminal(self: object, session_id: str, terminal_id: str) -> None:
        del self, session_id, terminal_id
        return None

    monkeypatch.setattr(runner_app_mod, "_auto_create_codex_terminal", _boom)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _no_terminal)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
        )

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "native_terminal_start_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_name", ["pi", "grok"])
async def test_native_terminal_ensure_returns_existing_resource(
    terminal_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure paths return an existing live terminal without re-creating."""
    sid = f"conv_{terminal_name}_existing"
    existing = SessionResourceView(
        id=terminal_resource_id(terminal_name, "main"),
        type="terminal",
        session_id=sid,
        name=f"live-{terminal_name}",
    )

    async def _stub_get(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        del self
        if session_id == sid and terminal_id == existing.id:
            return existing
        return None

    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _stub_get)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json={
                "terminal": terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )

    assert resp.status_code == 200
    assert resp.json()["name"] == f"live-{terminal_name}"


@pytest.mark.asyncio
async def test_generic_terminal_launch_runtime_error_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic terminal launches surface RuntimeError as terminal_launch_failed."""

    async def _boom(*_args: object, **_kwargs: object) -> SessionResourceView:
        raise RuntimeError("tmux refused")

    monkeypatch.setattr(SessionResourceRegistry, "launch_auxiliary_terminal", _boom)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_launch_fail/resources/terminals",
            json={"terminal": "bash", "session_key": "main"},
        )

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "terminal_launch_failed"