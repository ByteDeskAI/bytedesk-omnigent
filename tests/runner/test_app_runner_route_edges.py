"""Route-level edge coverage for nested helpers inside :func:`create_runner_app`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata
from omnigent.spec.types import AgentSpec, CompactionConfig, ExecutorSpec
from tests.runner.helpers import NullServerClient


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