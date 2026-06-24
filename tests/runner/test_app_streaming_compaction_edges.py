"""Edge coverage for reactive compaction, background turn drain, and related runner.app paths."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent import codex_native_bridge
from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    CompactionConfig,
    DatabricksAuth,
    ExecutorSpec,
    ProviderAuth,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_app_runner_route_edges import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.test_app_sessions_native import (
    _FakeServerClient,
    _OverflowThenSuccessHarnessClient,
    _build_interrupt_app,
    _runner_client,
)


def _overflow_frame() -> str:
    return _sse(
        {
            "type": "response.failed",
            "error": {
                "message": (
                    "context_length_exceeded: 5000 tokens > 4096 maximum context length"
                ),
                "code": "context_length_exceeded",
            },
        }
    )


class _CallCountingProcessManager(_FakeProcessManager):
    """Fails harness resolution after the first successful get_client."""

    def __init__(self, client: Any, *, fail_after: int = 1) -> None:
        super().__init__(client)
        self._get_client_calls = 0
        self._fail_after = fail_after
        self._fail_exception: Exception = RuntimeError("retry harness unavailable")

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None = None,
    ) -> Any:
        self._get_client_calls += 1
        if self._get_client_calls > self._fail_after:
            raise self._fail_exception
        return await super().get_client(conversation_id, harness, env)


class _AlwaysOverflowHarnessClient:
    """Harness that always returns context-window overflow on stream."""

    def __init__(self) -> None:
        self.posted_bodies: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = [_overflow_frame()]

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self_inner) -> Any:
                class _Handle:
                    status_code = 200

                    async def aiter_text(self_handle) -> AsyncIterator[str]:
                        for frame in frames:
                            yield frame

                return _Handle()

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        del url, timeout, json

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

        return _Response()


class _DrainErrorHarnessClient:
    """Harness stream that raises httpx.HTTPError mid-drain."""

    def __init__(self) -> None:
        self.posted_bodies: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)

        class _FailingHandle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": "resp_drain"}})
                raise httpx.ReadError("drain broke")

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self_inner) -> _FailingHandle:
                return _FailingHandle()

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        del url, timeout, json

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

        return _Response()


def _idle_history(count: int = 10) -> list[dict[str, Any]]:
    return [
        {
            "id": f"item_{i}",
            "type": "message",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "input_text", "text": f"msg {i}"}],
        }
        for i in range(count)
    ]


async def _wait_for_harness_posts(hc: Any, expected: int, *, timeout_s: float = 2.0) -> None:
    for _ in range(int(timeout_s * 100)):
        if len(hc.posted_bodies) >= expected:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"expected {expected} harness posts, got {len(hc.posted_bodies)}")


async def _wait_for_failed_status(conv: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
    queue = _session_event_queues_ref.get(conv)
    assert queue is not None
    for _ in range(int(timeout_s * 100)):
        events: list[dict[str, Any]] = []
        while not queue.empty():
            events.append(queue.get_nowait())
        for event in events:
            if event.get("type") == "session.status" and event.get("status") == "failed":
                return event
        await asyncio.sleep(0.01)
    pytest.fail("timed out waiting for session.status failed")


@pytest.mark.asyncio
async def test_background_turn_ends_failed_when_harness_spawn_fails() -> None:
    """Non-streaming harness error on the first turn publishes session.status failed."""
    conv = "conv_spawn_fail"
    spec = AgentSpec(spec_version=1, name="spawn-fail-agent")
    history = _idle_history()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    pm = _CallCountingProcessManager(_ScriptedHarnessClient([]), fail_after=1)  # type: ignore[arg-type]
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FakeServerClient(history),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "go"}],
                    },
                )
            ).status_code == 202

            failed = await _wait_for_failed_status(conv)
            assert "harness_spawn_failed" in (failed.get("error") or {}).get("message", "")
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_reactive_compaction_retry_non_streaming_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow retry that gets a non-streaming harness error ends the turn failed."""
    conv = "conv_retry_non_stream"
    spec = AgentSpec(spec_version=1, name="retry-non-stream")
    history = _idle_history()
    hc = _AlwaysOverflowHarnessClient()
    pm = _CallCountingProcessManager(hc, fail_after=2)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 100,
    )

    async def _fake_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[{"type": "message", "role": "user", "content": "compact"}],
            summary_metadata=SummaryMetadata(
                text="summary",
                last_item_id="item_last",
                model="test",
                token_count=3,
            ),
            total_tokens=3,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: MagicMock())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FakeServerClient(history),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "trigger"}],
                    },
                )
            ).status_code == 202

            await _wait_for_harness_posts(hc, 1)
            failed = await _wait_for_failed_status(conv)
            assert (failed.get("error") or {}).get("message") == (
                "Context window exceeded after compaction"
            )
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_reactive_compaction_double_overflow_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persistent overflow after compaction ends the turn with a failed status."""
    conv = "conv_double_overflow"
    spec = AgentSpec(spec_version=1, name="double-overflow")
    history = _idle_history()
    hc = _AlwaysOverflowHarnessClient()
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="omnigent.runner.app")

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 100,
    )

    async def _fake_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[{"type": "message", "role": "user", "content": "compact"}],
            summary_metadata=SummaryMetadata(
                text="summary",
                last_item_id="item_last",
                model="test",
                token_count=3,
            ),
            total_tokens=3,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: MagicMock())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FakeServerClient(history),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "trigger"}],
                    },
                )
            ).status_code == 202

            await _wait_for_harness_posts(hc, 2)
            failed = await _wait_for_failed_status(conv)
            assert (failed.get("error") or {}).get("message") == (
                "Context window exceeded after compaction"
            )
            assert "overflow persists after compaction" in caplog.text
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_reactive_compaction_retry_unexpected_exception_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected exception on post-compaction retry surfaces a failed turn."""
    conv = "conv_retry_exception"
    spec = AgentSpec(spec_version=1, name="retry-exception")
    history = _idle_history()
    success_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_unused"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_unused"}}),
    ]
    hc = _OverflowThenSuccessHarnessClient(success_frames)
    pm = _CallCountingProcessManager(hc, fail_after=2)  # type: ignore[arg-type]
    pm._fail_exception = ValueError("unexpected retry boom")
    caplog.set_level(logging.ERROR, logger="omnigent.runner.app")

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 100,
    )

    async def _fake_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[{"type": "message", "role": "user", "content": "compact"}],
            summary_metadata=SummaryMetadata(
                text="summary",
                last_item_id="item_last",
                model="test",
                token_count=3,
            ),
            total_tokens=3,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: MagicMock())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FakeServerClient(history),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "trigger"}],
                    },
                )
            ).status_code == 202

            await _wait_for_harness_posts(hc, 1)
            failed = await _wait_for_failed_status(conv)
            assert (failed.get("error") or {}).get("message") == (
                "Unexpected error on post-compaction retry"
            )
            assert "Unexpected error on post-compaction retry" in caplog.text
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_background_turn_drain_http_error_ends_failed() -> None:
    """httpx.HTTPError while draining a background stream ends the turn failed."""
    conv = "conv_drain_fail"
    spec = AgentSpec(spec_version=1, name="drain-fail")
    history = _idle_history()
    hc = _DrainErrorHarnessClient()
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_FakeServerClient(history),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": "ag_1"},
                )
            ).status_code == 201
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "go"}],
                    },
                )
            ).status_code == 202

            await _wait_for_harness_posts(hc, 1)
            failed = await _wait_for_failed_status(conv)
            assert (failed.get("error") or {}).get("message") == (
                "Harness stream connection error."
            )
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_eager_mcp_spec_resolution_failure_emits_response_failed() -> None:
    """MCP turns with a failing spec resolver emit response.failed without POSTing harness."""
    conv = "conv_eager_spec_fail"
    hc = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        if agent_id == "ag_mcp_turn":
            raise RuntimeError("resolver failed on turn")
        return AgentSpec(spec_version=1, name="mcp-agent")

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
                "agent_id": "ag_mcp_turn",
                "model": "test",
                "harness": "openai-agents",
                "has_mcp_servers": True,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        stream_text = ""
        async for chunk in resp.aiter_text():
            stream_text += chunk

    assert resp.status_code == 200
    assert '"type": "response.failed"' in stream_text or '"type":"response.failed"' in stream_text
    assert "Failed to resolve the agent spec for this turn." in stream_text
    assert hc.posted_bodies == []


@pytest.mark.asyncio
async def test_proactive_compaction_uses_executor_config_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compaction prefers executor.config.connection over summarize auth resolution."""
    conv = "conv_config_connection"
    spec = AgentSpec(
        spec_version=1,
        name="config-conn-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={
                "harness": "openai-agents",
                "connection": {"api_key": "cfg-key", "base_url": "https://cfg.example/v1"},
            },
        ),
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_cfg"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_cfg"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)
    captured: dict[str, Any] = {}

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
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two"}]},
    ] * 50

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 70000,
    )

    async def _fake_compact(*_args: Any, **kwargs: Any) -> CompactionResult:
        captured["connection"] = kwargs.get("connection")
        return CompactionResult(
            messages=[{"type": "message", "role": "user", "content": "compact"}],
            summary_metadata=SummaryMetadata(
                text="summary",
                last_item_id="item_last",
                model="gpt-4o-mini",
                token_count=12,
            ),
            total_tokens=12,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: MagicMock())

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": conv, "agent_id": "ag_compact"},
            )
            await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "gpt-4o-mini",
                    "agent_id": "ag_compact",
                    "content": [{"type": "input_text", "text": "next"}],
                },
            )
            await _wait_for_harness_posts(harness_client, 1)

        assert captured["connection"] == {
            "api_key": "cfg-key",
            "base_url": "https://cfg.example/v1",
        }
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_interrupt_skips_function_calls_without_call_id() -> None:
    """function_call items missing call_id are ignored during cancellation cleanup."""
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)
    conv_id = "conv_no_call_id"
    _session_histories_ref[conv_id] = [
        {"type": "function_call", "name": "orphan_no_id", "arguments": "{}"},
        {
            "type": "function_call",
            "call_id": "call_real",
            "name": "read_file",
            "arguments": "{}",
        },
    ]

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "test-agent",
                        "content": [{"type": "input_text", "text": "go"}],
                        "harness": "openai-agents",
                    },
                )
            ).status_code == 202
            await asyncio.sleep(0.1)
            assert (
                await client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={"type": "interrupt"},
                )
            ).status_code in (200, 204)
            gate.set()
            await asyncio.sleep(0.2)

        histories = _session_histories_ref.get(conv_id, [])
        synthetic_outputs = [
            h
            for h in histories
            if h.get("type") == "function_call_output"
            and h.get("output") == "[Cancelled — tool execution was interrupted.]"
        ]
        assert "call_real" in {o.get("call_id") for o in synthetic_outputs}
        assert all(o.get("call_id") for o in synthetic_outputs)
    finally:
        _session_histories_ref.pop(conv_id, None)


class _PendingElicitationServerClient(NullServerClient):
    """Server client that returns pending native approvals on session GET."""

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self._pending = pending or []

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.startswith("/v1/sessions/") and "/items" not in url and "/labels" not in url:
            pending = self._pending

            class _Resp:
                status_code = 200

                def json(self) -> dict[str, Any]:
                    return {"pending_elicitations": pending}

            return _Resp()  # type: ignore[return-value]
        return await super().get(url, **kwargs)


def test_terminal_attach_repop_pending_cost_popup_codex_native(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach re-pops a pending native approval onto the tmux client."""
    captured: list[tuple[Any, ...]] = []

    def _fake_wait(*_a: Any, **_k: Any) -> bool:
        return True

    def _fake_launch(
        socket_path: str,
        tmux_target: str,
        config_file: Any,
        *,
        session_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None,
    ) -> None:
        captured.append(
            (
                socket_path,
                tmux_target,
                config_file,
                session_id,
                elicitation_id,
                message,
                policy_name,
            )
        )

    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", _fake_wait)
    monkeypatch.setattr("omnigent.native_cost_popup.launch_cost_popup", _fake_launch)

    async def _slow_bridge(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0.5)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_codex"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    pending = [
        {
            "elicitation_id": "elicit_repop",
            "params": {
                "phase": "llm_request",
                "message": "Budget checkpoint",
                "policy_name": "cost-cap",
            },
        }
    ]
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PendingElicitationServerClient(pending),  # type: ignore[arg-type]
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

    assert len(captured) == 1
    socket_path, tmux_target, config_file, session_id, elicitation_id, message, policy_name = (
        captured[0]
    )
    assert socket_path == str(instance.socket_path)
    assert tmux_target == "main"
    assert config_file == (
        codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
        / codex_native_bridge._POLICY_HOOK_FILE
    )
    assert session_id == conv_id
    assert elicitation_id == "elicit_repop"
    assert message == "Budget checkpoint"
    assert policy_name == "cost-cap"


def test_terminal_attach_repop_skips_when_no_tmux_client(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach does not launch a popup when no tmux client registers in time."""
    launched: list[str] = []

    monkeypatch.setattr(
        "omnigent.native_cost_popup.wait_for_tmux_client",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-skip",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_skip"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PendingElicitationServerClient(
            [
                {
                    "elicitation_id": "elicit_skip",
                    "params": {"phase": "tool_call", "message": "Approve"},
                }
            ]
        ),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    def fake_fork() -> tuple[int, int]:
        return 0, 0

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        del path, argv, env
        raise OSError("stop child path")

    exit_exc = RuntimeError("child exited")
    monkeypatch.setattr("omnigent.terminals.ws_bridge.pty.fork", fake_fork)
    monkeypatch.setattr("omnigent.terminals.ws_bridge.os.execve", fake_execve)
    monkeypatch.setattr(
        "omnigent.terminals.ws_bridge.os._exit",
        lambda code: (_ for _ in ()).throw(exit_exc),
    )

    client = TestClient(app)
    assert client.post(
        "/v1/sessions",
        json={"session_id": conv_id, "agent_id": "ag_repop_skip"},
    ).status_code == 201

    with pytest.raises(RuntimeError, match="child exited"):
        with client.websocket_connect(
            f"/v1/sessions/{conv_id}/resources/terminals/terminal_codex_main/attach"
        ):
            pass

    assert launched == []


@pytest.mark.asyncio
async def test_summarize_uses_global_databricks_auth_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global Databricks auth from config.yaml is used when the spec declares none."""
    spec = AgentSpec(
        spec_version=1,
        name="global-db-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="databricks/databricks-gpt-5",
            config={"harness": "openai-agents"},
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
        "omnigent.runtime.workflow._load_global_auth",
        lambda: DatabricksAuth(profile="global-db"),
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(
            host=f"https://{profile}.example",
            token=f"tok-{profile}",
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_global_db"

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "databricks/databricks-gpt-5",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"]["api_key"] == "tok-global-db"


@pytest.mark.asyncio
async def test_summarize_provider_databricks_kind_resolves_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider entries with kind=databricks route through profile resolution."""
    spec = AgentSpec(
        spec_version=1,
        name="provider-db-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="databricks/databricks-gpt-5",
            auth=ProviderAuth(name="my-databricks"),
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="provider db")])],
            )

    class _DatabricksProvider:
        kind = "databricks"
        profile = "provider-profile"

        def family(self, name: str) -> Any:
            del name
            return None

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: {})
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_providers",
        lambda _cfg: {"my-databricks": _DatabricksProvider()},
    )
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(
            host=f"https://{profile}.example",
            token=f"tok-{profile}",
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_provider_db"

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "databricks/databricks-gpt-5",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"]["api_key"] == "tok-provider-profile"


@pytest.mark.asyncio
async def test_summarize_databricks_profile_resolution_failure_returns_none_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError resolving a Databricks profile yields None connection for summarize."""
    spec = AgentSpec(
        spec_version=1,
        name="db-fail-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="databricks/databricks-gpt-5",
            auth=DatabricksAuth(profile="missing"),
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="no creds")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )

    def _raise_oserror(profile: str) -> None:
        del profile
        raise OSError("no databrickscfg")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        _raise_oserror,
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_summarize_db_fail"

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "databricks/databricks-gpt-5",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"] is None