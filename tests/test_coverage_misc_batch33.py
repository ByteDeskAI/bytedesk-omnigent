"""Batch-33 coverage for sessions adapter send/cancel/tool/elicitation paths."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent_client import OmnigentError
from omnigent_client._events import ResponseCompleted
from omnigent_client._tool_handler import StreamHooks


@dataclass
class _StubSession:
    id: str
    agent_id: str
    runner_id: str | None = None
    status: str = "idle"


def _adapter(**kwargs: object) -> _SessionsChatReplAdapter:
    client = MagicMock()
    defaults: dict[str, object] = {
        "client": client,
        "agent_name": "test-agent",
        "session_bundle": b"bundle",
        "runner_id": "runner_1",
    }
    defaults.update(kwargs)
    return _SessionsChatReplAdapter(**defaults)  # type: ignore[arg-type]


def _wire_send_prereqs(adapter: _SessionsChatReplAdapter, session_id: str = "conv_1") -> None:
    adapter._ensure_session = AsyncMock(return_value=session_id)  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._session_id = session_id


@pytest.mark.asyncio
async def test_send_posts_message_and_yields_completed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter()
    _wire_send_prereqs(adapter)
    adapter._model_override = "claude-opus-4-7"

    async def _complete_turn(_session_id: str, _payload: dict[str, object]) -> None:
        adapter._turn_done.set()

    adapter._client.sessions.post_event = AsyncMock(side_effect=_complete_turn)
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    events = [event async for event in adapter.send("hello world")]

    assert len(events) == 1
    assert isinstance(events[0], ResponseCompleted)
    posted = adapter._client.sessions.post_event.await_args.args[1]
    assert posted["model_override"] == "claude-opus-4-7"
    assert posted["data"]["content"] == [{"type": "input_text", "text": "hello world"}]
    assert adapter.is_streaming is False
    captured = capsys.readouterr()
    assert "POST /events session=conv_1" in captured.err


@pytest.mark.asyncio
async def test_send_polls_snapshot_when_turn_done_not_signaled() -> None:
    adapter = _adapter()
    _wire_send_prereqs(adapter)
    adapter._client.sessions.post_event = AsyncMock()
    adapter._client.sessions.get = AsyncMock(
        return_value=_StubSession(id="conv_1", agent_id="ag_1", status="idle"),
    )

    events = [event async for event in adapter.send("ping")]

    assert len(events) == 1
    adapter._client.sessions.get.assert_awaited()


@pytest.mark.asyncio
async def test_send_attaches_uploaded_files(tmp_path: Path) -> None:
    adapter = _adapter()
    _wire_send_prereqs(adapter)

    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    text_path = tmp_path / "notes.md"
    text_path.write_text("# hello", encoding="utf-8")

    uploaded = MagicMock()
    uploaded.id = "file_123"

    session_files = MagicMock()
    session_files.upload = AsyncMock(return_value=uploaded)
    adapter._client.files.for_session = MagicMock(return_value=session_files)

    async def _complete_turn(_session_id: str, _payload: dict[str, object]) -> None:
        adapter._turn_done.set()

    adapter._client.sessions.post_event = AsyncMock(side_effect=_complete_turn)

    await anext(adapter.send("see attachments", files=[str(image_path), str(text_path)]))

    payload = adapter._client.sessions.post_event.await_args.args[1]
    blocks = payload["data"]["content"]
    assert blocks[0] == {"type": "input_text", "text": "see attachments"}
    assert blocks[1] == {"type": "input_image", "file_id": "file_123"}
    assert blocks[2] == {
        "type": "input_file",
        "file_id": "file_123",
        "filename": "notes.md",
    }


@pytest.mark.asyncio
async def test_send_accepts_prebuilt_content_blocks() -> None:
    adapter = _adapter()
    _wire_send_prereqs(adapter)
    adapter._client.sessions.post_event = AsyncMock(
        side_effect=lambda _sid, _payload: adapter._turn_done.set(),
    )

    blocks = [{"type": "input_text", "text": "structured"}]
    await anext(adapter.send(blocks))  # type: ignore[arg-type]

    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert payload["data"]["content"] == blocks


@pytest.mark.asyncio
async def test_send_skill_slash_command_posts_skill_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _adapter()
    _wire_send_prereqs(adapter)
    adapter._model_override = "claude-sonnet"
    adapter._client.sessions.post_event = AsyncMock(
        side_effect=lambda _sid, _payload: adapter._turn_done.set(),
    )
    monkeypatch.setenv("OMNIGENT_SESSIONS_ADAPTER_DEBUG", "1")

    events = [
        event
        async for event in adapter.send_skill_slash_command("code-review", "diff HEAD~1")
    ]

    assert len(events) == 1
    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert payload["type"] == "slash_command"
    assert payload["data"] == {
        "kind": "skill",
        "name": "code-review",
        "arguments": "diff HEAD~1",
    }
    assert adapter._pending_local_skill_slash_commands == []
    captured = capsys.readouterr()
    assert "POST skill slash command session=conv_1" in captured.err


@pytest.mark.asyncio
async def test_cancel_noop_without_session() -> None:
    adapter = _adapter()
    result = await adapter.cancel()
    assert result is None
    adapter._client.sessions.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_interrupts_active_session() -> None:
    adapter = _adapter(session_id="conv_1")
    adapter._client.sessions.interrupt = AsyncMock()
    await adapter.cancel()
    adapter._client.sessions.interrupt.assert_awaited_once_with("conv_1")


@pytest.mark.asyncio
async def test_spawn_client_tool_executes_sync_callable_and_posts_output() -> None:
    adapter = _adapter(tool_callables={"echo": lambda call: f"got:{call.arguments['msg']}"})
    adapter._current_response_id = "resp_1"
    adapter._client.sessions.post_event = AsyncMock()

    adapter._spawn_client_tool("conv_1", "call_1", "echo", json.dumps({"msg": "hi"}))
    await asyncio.sleep(0)

    adapter._client.sessions.post_event.assert_awaited_once()
    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert payload["type"] == "function_call_output"
    assert payload["data"]["output"] == "got:hi"


@pytest.mark.asyncio
async def test_spawn_client_tool_executes_async_callable() -> None:
    async def _async_echo(call: object) -> str:
        return "async-ok"

    adapter = _adapter(tool_callables={"echo": _async_echo})
    adapter._client.sessions.post_event = AsyncMock()
    adapter._spawn_client_tool("conv_1", "call_2", "echo", "{}")
    await asyncio.sleep(0)

    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert payload["data"]["output"] == "async-ok"


def test_spawn_client_tool_noop_for_unknown_tool() -> None:
    adapter = _adapter(tool_callables={})
    adapter._spawn_client_tool("conv_1", "call_3", "missing", "{}")
    assert adapter._pending_local_tasks == {}


@pytest.mark.asyncio
async def test_spawn_client_tool_surfaces_execution_errors() -> None:
    def _boom(_call: object) -> str:
        raise RuntimeError("tool exploded")

    adapter = _adapter(tool_callables={"boom": _boom})
    adapter._client.sessions.post_event = AsyncMock()
    adapter._spawn_client_tool("conv_1", "call_4", "boom", "not-json")
    await asyncio.sleep(0)

    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert "Error executing tool" in payload["data"]["output"]


@pytest.mark.asyncio
async def test_handle_elicitation_declines_without_hook() -> None:
    adapter = _adapter(hooks=StreamHooks(on_elicitation_request=None))
    event = MagicMock(
        elicitation_id="el_1",
        message="approve?",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="gate",
        content_preview="",
        url=None,
    )
    adapter._client.sessions.resolve_elicitation = AsyncMock(return_value={"queued": False})

    await adapter._handle_elicitation("conv_1", event)

    result = adapter._client.sessions.resolve_elicitation.await_args.args[2]
    assert result["action"] == "decline"


@pytest.mark.asyncio
async def test_handle_elicitation_accepts_boolean_schema_via_hook() -> None:
    async def _approve(_ctx: object) -> bool:
        return True

    adapter = _adapter(hooks=StreamHooks(on_elicitation_request=_approve))
    event = MagicMock(
        elicitation_id="el_2",
        message="",
        requested_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        },
        mode="form",
        phase="request",
        policy_name="gate",
        content_preview="",
        url=None,
    )
    adapter._client.sessions.resolve_elicitation = AsyncMock(return_value={"queued": False})

    await adapter._handle_elicitation("conv_1", event)

    result = adapter._client.sessions.resolve_elicitation.await_args.args[2]
    assert result["action"] == "accept"
    assert result["content"] == {"ok": True}


@pytest.mark.asyncio
async def test_handle_elicitation_declines_complex_schema() -> None:
    async def _approve(_ctx: object) -> bool:
        return True

    adapter = _adapter(hooks=StreamHooks(on_elicitation_request=_approve))
    event = MagicMock(
        elicitation_id="el_3",
        message="",
        requested_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
        mode="form",
        phase="request",
        policy_name="gate",
        content_preview="",
        url=None,
    )
    adapter._client.sessions.resolve_elicitation = AsyncMock(return_value={"queued": False})

    await adapter._handle_elicitation("conv_1", event)

    result = adapter._client.sessions.resolve_elicitation.await_args.args[2]
    assert result["action"] == "decline"


@pytest.mark.asyncio
async def test_handle_elicitation_ignores_already_resolved_elicitation() -> None:
    adapter = _adapter()
    event = MagicMock(
        elicitation_id="el_4",
        message="",
        requested_schema={},
        mode="form",
        phase="request",
        policy_name="gate",
        content_preview="",
        url=None,
    )
    adapter._client.sessions.resolve_elicitation = AsyncMock(
        side_effect=OmnigentError("gone", code="not_found", status_code=404),
    )

    await adapter._handle_elicitation("conv_1", event)


@pytest.mark.asyncio
async def test_handle_elicitation_reraises_unexpected_errors() -> None:
    adapter = _adapter()
    event = MagicMock(
        elicitation_id="el_5",
        message="",
        requested_schema={},
        mode="form",
        phase="request",
        policy_name="gate",
        content_preview="",
        url=None,
    )
    adapter._client.sessions.resolve_elicitation = AsyncMock(
        side_effect=OmnigentError("boom", code="internal", status_code=500),
    )

    with pytest.raises(OmnigentError, match="boom"):
        await adapter._handle_elicitation("conv_1", event)


@pytest.mark.asyncio
async def test_stream_pump_doubles_backoff_after_repeated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(session_id="conv_1")
    attempts = 0

    async def _stream(_session_id: str):
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"disconnect-{attempts}")
        yield  # pragma: no cover

    adapter._client.sessions.stream = _stream  # type: ignore[method-assign]
    sleeps: list[float] = []

    async def _track_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", _track_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._stream_pump()

    assert sleeps[:2] == [0.5, 1.0]