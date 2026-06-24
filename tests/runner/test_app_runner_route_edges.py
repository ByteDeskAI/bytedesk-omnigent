"""Route-level edge coverage for nested helpers inside :func:`create_runner_app`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent import claude_native_bridge, codex_native_bridge
from omnigent.claude_native_bridge import bridge_dir_for_conversation_id
from omnigent.runner import create_runner_app
from omnigent.runner import app as runner_app
from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata
from omnigent.spec.types import AgentSpec, CompactionConfig, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance


def _sse(event: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(event)}\n\n"


class _ScriptedHarnessClient:
    def __init__(self, sse_frames: list[str]) -> None:
        self.posted_bodies: list[dict[str, Any]] = []
        self._sse_frames = sse_frames

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self) -> Any:
                class _Handle:
                    status_code = 200

                    async def aiter_text(self) -> AsyncIterator[str]:
                        for frame in frames:
                            yield frame

                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        del url, json, timeout

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}
            content = b""

            def raise_for_status(self) -> None:
                pass

        return _Response()


class _FakeProcessManager:
    handles_tool_dispatch = True

    def __init__(self, client: _ScriptedHarnessClient) -> None:
        self._client = client
        self._sessions: set[str] = set()
        self._active_turns: set[str] = set()

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def has_active_turn(self, session_id: str) -> bool:
        return session_id in self._active_turns

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None = None,
    ) -> _ScriptedHarnessClient:
        del harness, env
        self._sessions.add(conversation_id)
        return self._client

    async def release(self, conversation_id: str) -> None:
        self._sessions.discard(conversation_id)


@pytest.fixture
async def runner_client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_runner_app(server_client=NullServerClient()))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


@pytest.mark.asyncio
async def test_has_active_work_false_without_process_manager() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    assert app.state.has_active_work() is False


@pytest.mark.asyncio
async def test_proactive_compaction_publishes_lifecycle_and_persists_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = "conv_proactive_compact"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={"harness": "openai-agents", "model": "gpt-4o-mini"},
        ),
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_compact"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_compact"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    compaction_posts: list[dict[str, Any]] = []

    class _RecordingServerClient(NullServerClient):
        async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            if url.endswith("/events"):
                compaction_posts.append(kwargs.get("json", {}))
            return await super().post(url, **kwargs)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_RecordingServerClient(),  # type: ignore[arg-type]
    )

    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two"}]},
    ] * 50

    # Default context window is 128k; threshold 0.5 → budget 64k — exceed it.
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 70000,
    )

    async def _fake_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[{"type": "message", "role": "user", "content": "compact"}],
            summary_metadata=SummaryMetadata(
                text="summarized context",
                last_item_id="item_last",
                model="gpt-4o-mini",
                token_count=12,
            ),
            total_tokens=12,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _fake_compact)
    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: MagicMock(),
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create = await client.post(
                "/v1/sessions",
                json={"session_id": conv, "agent_id": "ag_compact"},
            )
            assert create.status_code == 201

            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "gpt-4o-mini",
                    "agent_id": "ag_compact",
                    "content": [{"type": "input_text", "text": "next turn"}],
                },
            )
            assert resp.status_code == 202

            for _ in range(300):
                if harness_client.posted_bodies:
                    break
                await asyncio.sleep(0.01)

        events: list[dict[str, Any]] = []
        for _ in range(300):
            while not queue.empty():
                events.append(queue.get_nowait())
            event_types = [e.get("type") for e in events]
            if (
                "response.compaction.in_progress" in event_types
                and "response.compaction.completed" in event_types
            ):
                break
            await asyncio.sleep(0.01)

        event_types = [e.get("type") for e in events]
        assert "response.compaction.in_progress" in event_types
        assert "response.compaction.completed" in event_types
        assert any(
            post.get("type") == "compaction" for post in compaction_posts
        ), f"expected compaction persist POST, got {compaction_posts}"
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_proactive_compaction_layer2_serializes_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Layer-2 compaction uses _serialize_messages_as_summary when LLM summary is absent."""
    import logging

    conv = "conv_layer2_compact"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={"harness": "openai-agents"},
        ),
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_l2"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_l2"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "lookup",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 250},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ] * 40

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 70000,
    )

    async def _layer2_compact(*_args: Any, **_kwargs: Any) -> CompactionResult:
        return CompactionResult(
            messages=[
                {"type": "message", "role": "user", "content": "u"},
                {"type": "function_call", "name": "tool_x"},
                {"type": "function_call_output", "output": "result"},
            ],
            summary_metadata=None,
            total_tokens=3,
        )

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _layer2_compact)
    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: MagicMock(),
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    caplog.set_level(logging.WARNING, logger="omnigent.runner.app")
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
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
                    "content": [{"type": "input_text", "text": "go"}],
                },
            )
            for _ in range(300):
                if harness_client.posted_bodies:
                    break
                await asyncio.sleep(0.01)

        assert "Skipping compaction persist" in caplog.text
        event_types = []
        while not queue.empty():
            event_types.append(queue.get_nowait().get("type"))
        assert "response.compaction.completed" in event_types
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_proactive_compaction_failure_still_publishes_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = "conv_compact_fail"
    spec = AgentSpec(
        spec_version=1,
        name="compact-agent",
        compaction=CompactionConfig(trigger_threshold=0.5, recent_window=0),
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            config={"harness": "openai-agents"},
        ),
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_fail"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_fail"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    _session_histories_ref[conv] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]},
    ] * 80

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 70000,
    )

    async def _boom(*_args: Any, **_kwargs: Any) -> CompactionResult:
        raise RuntimeError("compact exploded")

    monkeypatch.setattr("omnigent.runtime.compaction.compact", _boom)
    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: MagicMock(),
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref[conv] = queue

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
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
                    "content": [{"type": "input_text", "text": "go"}],
                },
            )
            for _ in range(300):
                events = []
                while not queue.empty():
                    events.append(queue.get_nowait())
                if any(e.get("type") == "response.compaction.completed" for e in events):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("expected response.compaction.completed after compaction failure")
    finally:
        _session_histories_ref.pop(conv, None)
        _session_event_queues_ref.pop(conv, None)


class _PaginatedServerClient:
    """Minimal paginated history server stub for history-loader tests."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, timeout: float = 10.0
    ) -> Any:
        del url, timeout
        params = params or {}
        after = params.get("after")
        limit = int(params.get("limit", "100"))
        start = 0
        if after:
            for i, item in enumerate(self._items):
                if item.get("id") == after:
                    start = i + 1
                    break
        page = self._items[start : start + limit]
        has_more = (start + limit) < len(self._items)

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"data": page, "has_more": has_more}

        return _Resp()


@pytest.mark.asyncio
async def test_events_cost_approval_popup_claude_native_dispatches_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[Any, ...]] = []

    def _fake_popup(
        bridge_dir: Any,
        *,
        session_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None,
        timeout_s: float,
    ) -> None:
        captured.append(
            (bridge_dir, session_id, elicitation_id, message, policy_name, timeout_s)
        )

    monkeypatch.setattr(claude_native_bridge, "display_cost_approval_popup", _fake_popup)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    conv_id = "conv_claude_cost_popup"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        assert (
            await client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "ag_1"},
            )
        ).status_code == 201
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_abc",
                "message": "Budget exceeded",
                "policy_name": "cost-cap",
            },
        )

    assert resp.status_code == 204, resp.text
    assert len(captured) == 1
    bridge_dir, session_id, elicitation_id, message, policy_name, timeout_s = captured[0]
    assert bridge_dir == bridge_dir_for_conversation_id(conv_id)
    assert session_id == conv_id
    assert elicitation_id == "elicit_abc"
    assert message == "Budget exceeded"
    assert policy_name == "cost-cap"
    assert timeout_s == 1.0


@pytest.mark.asyncio
async def test_events_cost_approval_popup_claude_native_returns_503_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("tmux popup unavailable")

    monkeypatch.setattr(claude_native_bridge, "display_cost_approval_popup", _boom)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    conv_id = "conv_claude_cost_popup_fail"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_fail",
                "message": "Approve spend",
            },
        )

    assert resp.status_code == 503
    body = resp.json()
    assert body.get("error") == "claude_native_cost_popup_failed"


@pytest.mark.asyncio
async def test_events_cost_approval_popup_codex_native_launches_popup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    captured: list[tuple[Any, ...]] = []

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

    monkeypatch.setattr("omnigent.native_cost_popup.launch_cost_popup", _fake_launch)

    codex_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_codex_cost_popup"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_codex",
                "message": "Continue?",
            },
        )

    assert resp.status_code == 204, resp.text
    assert len(captured) == 1
    socket_path, tmux_target, config_file, session_id, elicitation_id, message, policy_name = (
        captured[0]
    )
    assert socket_path == str(instance.socket_path)
    assert tmux_target == "main"
    assert config_file == codex_native_bridge.bridge_dir_for_bridge_id(conv_id) / codex_native_bridge._POLICY_HOOK_FILE
    assert session_id == conv_id
    assert elicitation_id == "elicit_codex"
    assert message == "Continue?"
    assert policy_name is None


@pytest.mark.asyncio
async def test_events_cost_approval_popup_codex_native_returns_204_without_terminal() -> None:
    codex_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_codex_cost_popup_no_term"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_no_term",
                "message": "Approve",
            },
        )

    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_events_cost_approval_popup_codex_native_returns_503_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("popup launch failed")

    monkeypatch.setattr("omnigent.native_cost_popup.launch_cost_popup", _boom)

    codex_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return codex_spec

    conv_id = "conv_codex_cost_popup_fail"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_codex_fail",
                "message": "Approve",
            },
        )

    assert resp.status_code == 503
    assert resp.json().get("error") == "codex_native_cost_popup_failed"


@pytest.mark.asyncio
async def test_events_cost_approval_popup_rejects_missing_elicitation_id() -> None:
    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    conv_id = "conv_cost_popup_bad_body"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "cost_approval_popup", "message": "missing id"},
        )

    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_input"


@pytest.mark.asyncio
async def test_events_cost_approval_popup_non_native_is_204_noop() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    conv_id = "conv_cost_popup_noop"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post("/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"})
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "cost_approval_popup",
                "elicitation_id": "elicit_noop",
                "message": "ignored",
            },
        )

    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_history_load_converts_tool_items_and_skips_unknown_types(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "run tool"}],
        },
        {
            "id": "item_2",
            "type": "function_call",
            "call_id": "call_x",
            "name": "lookup",
            "arguments": '{"q":"x"}',
        },
        {"id": "item_3", "type": "reasoning", "text": "thinking"},
        {
            "id": "item_4",
            "type": "function_call_output",
            "call_id": "call_x",
            "output": "result",
        },
        {
            "id": "item_5",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]
    server_client = _PaginatedServerClient(history)
    spec = AgentSpec(spec_version=1, name="history-tools")
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_h"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_h"}}),
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

    caplog.set_level(logging.WARNING, logger="omnigent.runner.app")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        assert (
            await client.post(
                "/v1/sessions",
                json={"session_id": "conv_tool_history", "agent_id": "ag_1"},
            )
        ).status_code == 201
        assert (
            await client.post(
                "/v1/sessions/conv_tool_history/events",
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

    assert harness_client.posted_bodies
    content = harness_client.posted_bodies[0].get("content", [])
    types = [item.get("type") for item in content if isinstance(item, dict)]
    assert "function_call" in types
    assert "function_call_output" in types
    assert "reasoning" not in types
    assert "_convert_raw_items_to_input: skipped" in caplog.text


@pytest.mark.parametrize(
    ("history_content", "expected_preview"),
    [
        ("STRING_PREVIEW", "STRING_PREVIEW"),
        (
            [{"type": "input_text", "text": "BLOCK_PREVIEW"}],
            "BLOCK_PREVIEW",
        ),
    ],
)
@pytest.mark.asyncio
async def test_child_idle_status_uses_assistant_history_preview(
    history_content: str | list[dict[str, str]],
    expected_preview: str,
) -> None:
    parent_id = "conv_parent_hist_preview"
    child_id = f"conv_child_hist_preview_{expected_preview}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    _session_histories_ref[child_id] = [
        {"type": "message", "role": "assistant", "content": history_content},
    ]
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )

    publisher = app.state.session_resource_registry._session_status_publisher
    assert publisher is not None

    try:
        publisher(child_id, "idle")
        events: list[dict[str, Any]] = []
        queue = _session_event_queues_ref.get(parent_id)
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    events.append(item)
    finally:
        runner_app.unregister_child_session(child_id)
        _session_histories_ref.pop(child_id, None)
        _session_event_queues_ref.pop(parent_id, None)

    assert any(
        e.get("type") == "session.child_session.updated"
        and e.get("child", {}).get("last_message_preview") == expected_preview
        for e in events
    )