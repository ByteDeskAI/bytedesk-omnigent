"""Batch-34 coverage for remaining sessions adapter and REPL helper gaps."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.text import Text

from omnigent.repl._repl import (
    _SessionsChatReplAdapter,
    _clear_screen,
    _maybe_write_session_log,
)
from omnigent_client._tool_handler import StreamHooks


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)


@dataclass
class _StubSession:
    id: str
    agent_id: str
    runner_id: str | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    agent_name: str | None = "test-agent"
    llm_model: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    harness: str | None = None
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


@pytest.mark.asyncio
async def test_ensure_session_inner_lock_returns_when_peer_created_stream() -> None:
    adapter = _adapter()
    gate = asyncio.Event()
    create_calls = 0

    async def _slow_create(*_args: object, **_kwargs: object) -> _StubSession:
        nonlocal create_calls
        create_calls += 1
        gate.set()
        await asyncio.sleep(0.05)
        return _StubSession(id="conv_race", agent_id="ag_race")

    adapter._client.sessions.create = _slow_create
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock()  # type: ignore[method-assign]

    first = asyncio.create_task(adapter._ensure_session())
    await gate.wait()
    second = asyncio.create_task(adapter._ensure_session())
    session_a, session_b = await asyncio.gather(first, second)

    assert session_a == session_b == "conv_race"
    assert create_calls == 1


@pytest.mark.asyncio
async def test_send_attaches_files_to_existing_content_blocks(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter._ensure_session = AsyncMock(return_value="conv_1")  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]

    file_path = tmp_path / "doc.txt"
    file_path.write_text("body", encoding="utf-8")
    uploaded = MagicMock(id="file_doc")
    session_files = MagicMock()
    session_files.upload = AsyncMock(return_value=uploaded)
    adapter._client.files.for_session = MagicMock(return_value=session_files)
    adapter._client.sessions.post_event = AsyncMock(
        side_effect=lambda _sid, _payload: adapter._turn_done.set(),
    )

    blocks = [{"type": "input_text", "text": "preface"}]
    await anext(adapter.send(blocks, files=[str(file_path)]))  # type: ignore[arg-type]

    payload = adapter._client.sessions.post_event.await_args.args[1]
    assert payload["data"]["content"][0] == blocks[0]
    assert payload["data"]["content"][1]["type"] == "input_file"


@pytest.mark.asyncio
async def test_send_skill_slash_command_polls_snapshot_backstop() -> None:
    adapter = _adapter()
    adapter._ensure_session = AsyncMock(return_value="conv_1")  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._bind_runner_if_needed = AsyncMock()  # type: ignore[method-assign]
    adapter._client.sessions.post_event = AsyncMock()
    adapter._client.sessions.get = AsyncMock(
        return_value=_StubSession(id="conv_1", agent_id="ag_1", status="idle"),
    )

    events = [event async for event in adapter.send_skill_slash_command("lint", "")]
    assert len(events) == 1
    adapter._client.sessions.get.assert_awaited()


@pytest.mark.asyncio
async def test_handle_elicitation_declines_when_hook_raises() -> None:
    async def _broken_hook(_ctx: object) -> bool:
        raise RuntimeError("prompt failed")

    adapter = _adapter(hooks=StreamHooks(on_elicitation_request=_broken_hook))
    event = MagicMock(
        elicitation_id="el_6",
        message="",
        requested_schema={},
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


def test_clear_screen_prints_scroll_off_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []

    monkeypatch.setattr(
        "omnigent.repl._repl.os.get_terminal_size",
        lambda: os.terminal_size((80, 10)),
    )
    monkeypatch.setattr(
        "builtins.print",
        lambda text, end="", flush=False: printed.append(text),
    )

    _clear_screen()
    assert printed == ["\n" * 10]


def test_clear_screen_falls_back_when_terminal_size_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    def _raise() -> os.terminal_size:
        raise OSError("no tty")

    monkeypatch.setattr("omnigent.repl._repl.os.get_terminal_size", _raise)
    monkeypatch.setattr(
        "builtins.print",
        lambda text, end="", flush=False: printed.append(text),
    )

    _clear_screen()
    assert printed == ["\n" * 24]


@pytest.mark.asyncio
async def test_maybe_write_session_log_noop_without_conversation_id() -> None:
    host = _CaptureHost()
    session = MagicMock(session_id=None)
    client = MagicMock()
    fmt = MagicMock()

    await _maybe_write_session_log(client, session, "agent", Path("/tmp"), host, fmt)

    assert host.outputs == []


@pytest.mark.asyncio
async def test_maybe_write_session_log_surfaces_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _CaptureHost()
    session = MagicMock(session_id="conv_1")
    client = MagicMock()
    fmt = MagicMock(muted="dim")

    async def _boom(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("disk full")

    monkeypatch.setattr("omnigent.repl._session_log.write_session_log", _boom)

    await _maybe_write_session_log(client, session, "agent", Path("/tmp"), host, fmt)

    rendered = host.outputs[-1]
    assert isinstance(rendered, Text)
    assert "session log write failed" in rendered.plain


@pytest.mark.asyncio
async def test_maybe_write_session_log_reports_success_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = _CaptureHost()
    session = MagicMock(session_id="conv_1")
    client = MagicMock()
    fmt = MagicMock(muted="dim")
    log_path = tmp_path / "conv_1.json"

    async def _write(*_args: object, **_kwargs: object) -> Path:
        return log_path

    monkeypatch.setattr("omnigent.repl._session_log.write_session_log", _write)

    await _maybe_write_session_log(client, session, "agent", tmp_path, host, fmt)

    rendered = host.outputs[-1]
    assert isinstance(rendered, Text)
    assert str(log_path) in rendered.plain