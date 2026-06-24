"""Batch-5 edge coverage for omnigent.runner.app gaps (drain, policy, interrupt, MCP)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import StreamingResponse as _OrigStreamingResponse
from fastapi.testclient import TestClient

from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.terminals import TerminalRegistry
from omnigent.runner.app import (
    _session_agent_ids_ref,
    _session_event_queues_ref,
    _session_histories_ref,
)
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata
from omnigent.spec.types import AgentSpec, ExecutorSpec
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
    _advisor_orchestrator_spec,
    _advisor_note_items,
    _patch_judge_returns_pricey,
)
from tests.runner.test_app_streaming_compaction_edges import (
    _wait_for_failed_status,
    _wait_for_harness_posts,
)


class _BrokenStreamingResponse(_OrigStreamingResponse):
    """StreamingResponse whose body_iterator raises RuntimeError on drain."""

    def __init__(self, content: Any, **kwargs: Any) -> None:
        del content
        super().__init__(self._broken_body(), **kwargs)

    @staticmethod
    async def _broken_body() -> AsyncIterator[bytes]:
        raise RuntimeError("drain iterator failed")
        yield b""  # pragma: no cover


class _RecordingServerClient(NullServerClient):
    """Captures external_conversation_item posts for interrupt assertions."""

    def __init__(self) -> None:
        self.persisted_items: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.endswith("/events"):
            body = kwargs.get("json") or {}
            if body.get("type") == "external_conversation_item":
                data = body.get("data") or {}
                item_data = dict(data.get("item_data") or {})
                item_data["type"] = data.get("item_type")
                self.persisted_items.append(item_data)
        return await super().post(url, **kwargs)


class _PolicyHarnessClient(_ScriptedHarnessClient):
    """Harness that emits a policy_evaluation.requested frame mid-stream."""


def _policy_frames() -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": "resp_pol"}}),
        _sse(
            {
                "type": "policy_evaluation.requested",
                "evaluation_id": "peval_test_1",
                "phase": "llm_request",
                "data": {"model": "gpt-4o-mini"},
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_pol"}}),
    ]


class _FailingGetServerClient(NullServerClient):
    """Server client that fails session GET for repop edge coverage."""

    def __init__(self, *, status_code: int = 200, pending: list[dict[str, Any]] | None = None) -> None:
        self._status_code = status_code
        self._pending = pending or []
        self._fail_get = False

    async def get(self, url: str, **kwargs: Any) -> Any:
        if self._fail_get and url.startswith("/v1/sessions/") and "/items" not in url:
            raise httpx.ConnectError("session snapshot unavailable")

        if url.startswith("/v1/sessions/") and "/items" not in url and "/labels" not in url:
            pending = self._pending
            status_code = self._status_code

            class _Resp:
                def __init__(self) -> None:
                    self.status_code = status_code

                def json(self) -> dict[str, Any]:
                    return {"pending_elicitations": pending}

            return _Resp()

        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_background_turn_drain_runtime_error_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RuntimeError while draining a background stream hits the drain error path."""
    import logging

    conv = "conv_drain_runtime"
    spec = AgentSpec(spec_version=1, name="drain-runtime")
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "go"}],
        }
    ]
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_d"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_d"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="omnigent.runner.app")
    monkeypatch.setattr("omnigent.runner.app.StreamingResponse", _BrokenStreamingResponse)

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

            failed = await _wait_for_failed_status(conv)
            assert (failed.get("error") or {}).get("message") == (
                "background turn drain failed: drain iterator failed"
            )
            assert "drain failed for conv_drain_runtime" in caplog.text
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_reactive_compaction_seeds_context_without_prior_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Overflow on a turn with no pre-seeded compaction context creates one."""
    import logging

    conv = "conv_seed_ctx"
    spec = AgentSpec(spec_version=1, name="seed-ctx")
    success_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_seed"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_seed"}}),
    ]
    hc = _OverflowThenSuccessHarnessClient(success_frames)
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")

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
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: object())
    monkeypatch.setattr(
        "omnigent.llms.context_window.get_model_context_window",
        lambda _model: None,
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_1",
                        "model": "unknown-no-ctx-window",
                        "content": [{"type": "input_text", "text": "trigger"}],
                    },
                )
            ).status_code == 202

            await _wait_for_harness_posts(hc, 2)
            assert "Reactive compaction for session=conv_seed_ctx: 5000 > 4096" in caplog.text
    finally:
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_policy_evaluation_requested_dispatches_via_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy_evaluation.requested SSE frames dispatch _evaluate_policy_via_omnigent."""
    conv = "conv_policy_eval"
    spec = AgentSpec(spec_version=1, name="policy-agent")
    hc = _PolicyHarnessClient(_policy_frames())
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    evaluate_mock = AsyncMock()
    monkeypatch.setattr("omnigent.runner.app._evaluate_policy_via_omnigent", evaluate_mock)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_pol"})
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_pol",
                "model": "test",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 202
        for _ in range(200):
            if evaluate_mock.await_count:
                break
            await asyncio.sleep(0.01)

    evaluate_mock.assert_awaited_once()
    _kwargs = evaluate_mock.await_args.kwargs
    assert _kwargs["evaluation_id"] == "peval_test_1"
    assert _kwargs["phase"] == "llm_request"
    assert _kwargs["conversation_id"] == conv


class _BlockingHarnessAfterCalls(_ScriptedHarnessClient):
    """Blocks the harness stream after the first function_call frame."""

    def __init__(self, sse_frames: list[str], gate: asyncio.Event) -> None:
        super().__init__(sse_frames)
        self._gate = gate
        self.post_seen = asyncio.Event()

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        self.post_seen.set()
        frames = self._sse_frames
        gate = self._gate

        class _BlockingCtx:
            status_code = 200

            async def __aenter__(self) -> Any:
                class _Handle:
                    status_code = 200

                    async def aiter_text(self) -> AsyncIterator[str]:
                        for i, frame in enumerate(frames):
                            if i == 2:
                                await gate.wait()
                            yield frame

                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _BlockingCtx()


@pytest.mark.asyncio
async def test_interrupt_stamps_agent_name_on_persisted_dangling_calls() -> None:
    """Cancellation persists dangling function_calls with the cached agent name."""
    gate = asyncio.Event()
    spec = AgentSpec(spec_version=1, name="interrupt-agent")
    server_client = _RecordingServerClient()
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_int"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_a",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/x"}',
                },
            }
        ),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_b",
                    "name": "write_file",
                    "arguments": '{"path": "/tmp/y"}',
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_int"}}),
    ]
    harness_client = _BlockingHarnessAfterCalls(sse_frames, gate)
    pm = _FakeProcessManager(harness_client)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    conv_id = "conv_agent_stamp"

    try:
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions",
                    json={"session_id": conv_id, "agent_id": "ag_stamp"},
                )
            ).status_code == 201
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
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)
            for _ in range(100):
                histories = _session_histories_ref.get(conv_id, [])
                if any(h.get("type") == "function_call" for h in histories):
                    break
                await asyncio.sleep(0.02)
            assert (
                await client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={"type": "interrupt"},
                )
            ).status_code in (200, 204)
            for _ in range(100):
                persisted_calls = [
                    item
                    for item in server_client.persisted_items
                    if item.get("type") == "function_call"
                ]
                if persisted_calls:
                    break
                await asyncio.sleep(0.02)

        assert persisted_calls, "expected dangling function_call persistence on interrupt"
        assert all(item.get("agent") == "interrupt-agent" for item in persisted_calls)
    finally:
        _session_histories_ref.pop(conv_id, None)


@pytest.mark.asyncio
async def test_interrupt_skips_calls_that_already_have_output() -> None:
    """function_call_output entries remove call_ids from the cancellation set."""
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)
    conv_id = "conv_skip_output"
    _session_histories_ref[conv_id] = [
        {
            "type": "function_call",
            "call_id": "call_done",
            "name": "done_tool",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_done",
            "output": "already finished",
        },
        {
            "type": "function_call",
            "call_id": "call_a",
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

        synthetic = [
            h
            for h in _session_histories_ref.get(conv_id, [])
            if h.get("type") == "function_call_output"
            and h.get("output") == "[Cancelled — tool execution was interrupted.]"
        ]
        cancelled_ids = {h.get("call_id") for h in synthetic}
        assert "call_a" in cancelled_ids
        assert "call_done" not in cancelled_ids
    finally:
        _session_histories_ref.pop(conv_id, None)


@pytest.mark.asyncio
async def test_mcp_runner_local_lazy_spec_resolve() -> None:
    """Runner-local MCP tools resolve the agent spec lazily when cache is empty."""
    conv = "conv_lazy_spec"
    captured: dict[str, Any] = {}
    resolved = AgentSpec(spec_version=1, name="lazy-resolved-agent")

    async def _fake_execute_tool(*, tool_name: str, agent_spec: Any = None, **kwargs: Any) -> str:
        del kwargs
        captured["tool_name"] = tool_name
        captured["agent_spec"] = agent_spec
        return "ok"

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        assert agent_id == "ag_lazy"
        return resolved

    orig = tool_dispatch.execute_tool
    tool_dispatch.execute_tool = _fake_execute_tool  # type: ignore[assignment]
    _session_agent_ids_ref[conv] = "ag_lazy"

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        spec_resolver=_resolver,
    )

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "sys_os_read", "arguments": {"path": "/tmp/x"}},
                },
            )
        assert resp.status_code == 200
        assert captured["tool_name"] == "sys_os_read"
        assert getattr(captured["agent_spec"], "name", None) == "lazy-resolved-agent"
    finally:
        tool_dispatch.execute_tool = orig
        _session_agent_ids_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_mcp_runner_local_spec_resolver_failure_still_executes() -> None:
    """A failing lazy spec resolver does not block runner-local tool execution."""
    conv = "conv_lazy_fail"
    captured: dict[str, Any] = {}

    async def _fake_execute_tool(*, agent_spec: Any = None, **kwargs: Any) -> str:
        del kwargs
        captured["agent_spec"] = agent_spec
        return "ok"

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        raise RuntimeError("resolver blew up")

    orig = tool_dispatch.execute_tool
    tool_dispatch.execute_tool = _fake_execute_tool  # type: ignore[assignment]
    _session_agent_ids_ref[conv] = "ag_fail"

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        spec_resolver=_resolver,
    )

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "sys_os_read", "arguments": {}},
                },
            )
        assert resp.status_code == 200
        assert captured["agent_spec"] is None
    finally:
        tool_dispatch.execute_tool = orig
        _session_agent_ids_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_compaction_retry_remerges_advisor_note_without_session_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reactive compaction retry re-merges the advisor note when context was absent."""
    conv = "conv_adv_no_ctx"
    spec = _advisor_orchestrator_spec()
    success_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_adv"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_adv"}}),
    ]
    hc = _OverflowThenSuccessHarnessClient(success_frames)
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    _patch_judge_returns_pricey(monkeypatch)

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
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: object())

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
            assert (
                await client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_adv",
                        "model": "test",
                        "content": [{"type": "input_text", "text": "refactor auth"}],
                    },
                )
            ).status_code == 202
            await _wait_for_harness_posts(hc, 2)

        first, retry = hc.posted_bodies
        assert first.get("model_override") == "model-pricey"
        assert retry.get("model_override") == "model-pricey"
        assert len(_advisor_note_items(first.get("content") or [])) == 1
        assert _advisor_note_items(retry.get("content") or []) == _advisor_note_items(
            first.get("content") or []
        )
    finally:
        _session_histories_ref.pop(conv, None)


def test_terminal_attach_repop_skips_on_session_get_http_error(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repop exits quietly when the session snapshot GET fails."""
    launched: list[str] = []
    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )
    async def _slow_bridge(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-http",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    server_client = _FailingGetServerClient()
    server_client._fail_get = True

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_http"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
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


def test_terminal_attach_repop_skips_on_non_200_session_snapshot(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repop exits when the session snapshot returns a non-200 status."""
    launched: list[str] = []
    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )
    async def _slow_bridge(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-404",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    server_client = _FailingGetServerClient(status_code=404)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_404"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
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


def test_terminal_attach_repop_skips_when_pending_has_no_matching_phase(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repop ignores pending elicitations whose phase is not repop-eligible."""
    launched: list[str] = []
    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )
    async def _slow_bridge(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-phase",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    pending = [{"elicitation_id": "elicit_x", "params": {"phase": "completed", "message": "done"}}]
    server_client = _FailingGetServerClient(pending=pending)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_phase"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
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


def test_terminal_attach_repop_skips_when_elicitation_id_missing(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repop ignores pending entries without a usable elicitation_id."""
    launched: list[str] = []
    monkeypatch.setattr("omnigent.native_cost_popup.wait_for_tmux_client", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "omnigent.native_cost_popup.launch_cost_popup",
        lambda *_a, elicitation_id, **_k: launched.append(elicitation_id),
    )
    async def _slow_bridge(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_pty_to_websocket", _slow_bridge)

    codex_spec = AgentSpec(
        spec_version=1,
        name="codex-repop-id",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    pending = [{"params": {"phase": "tool_call", "message": "Approve"}}]
    server_client = _FailingGetServerClient(pending=pending)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_repop_id"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
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