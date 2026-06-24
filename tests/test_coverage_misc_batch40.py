"""Batch-40 coverage for /cancel, overview renderers, and debug overview paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omnigent_ui_sdk import OverlayTarget, RichBlockFormatter
from rich.console import Console

from omnigent.repl._repl import (
    COMMANDS,
    _build_call_id_to_name_lookup,
    _build_debug_overview,
    _coerce_arguments_dict,
    _extract_function_call_output_text,
    _extract_message_text,
    _render_overview_event,
    _render_overview_message_event,
    _terminal_target_key,
    _tool_metadata_from_function_call_item,
    handle_slash_command,
)
from tests.repl.helpers import CapturingHost


def _plain(*renderables: object) -> str:
    console = Console(record=True, width=120)
    for renderable in renderables:
        console.print(renderable)
    return console.export_text()


def _plain_list(renderables: list[object]) -> str:
    return _plain(*renderables)


@dataclass
class _CancelSession:
    model: str = "test-agent"
    cancel_result: object = None
    cancel_raises: BaseException | None = None

    async def cancel(self) -> object:
        if self.cancel_raises is not None:
            raise self.cancel_raises
        return self.cancel_result


@pytest.mark.asyncio
async def test_cancel_reports_cancelled_response_id() -> None:
    host = CapturingHost()
    cancelled = MagicMock(id="resp_cancelled")
    session = _CancelSession(cancel_result=cancelled)

    _, handler = COMMANDS["/cancel"]
    await handler("", session, MagicMock(), host, RichBlockFormatter())  # type: ignore[arg-type]

    assert "resp_cancelled" in host.text


@pytest.mark.asyncio
async def test_cancel_silent_when_nothing_to_cancel() -> None:
    host = CapturingHost()
    session = _CancelSession(cancel_result=None)

    await handle_slash_command(
        "/cancel",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,
        RichBlockFormatter(),
    )

    assert host.text == ""


@pytest.mark.asyncio
async def test_handle_slash_command_unknown_command() -> None:
    host = CapturingHost()
    await handle_slash_command(
        "/not-a-real-command",
        _CancelSession(),  # type: ignore[arg-type]
        MagicMock(),
        host,
        RichBlockFormatter(),
    )
    assert "Unknown command" in host.text


@pytest.mark.asyncio
async def test_handle_slash_command_catches_handler_exception() -> None:
    host = CapturingHost()

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("handler exploded")

    original = COMMANDS["/cancel"]
    COMMANDS["/cancel"] = (original[0], _boom)
    try:
        await handle_slash_command(
            "/cancel",
            _CancelSession(),  # type: ignore[arg-type]
            MagicMock(),
            host,
            RichBlockFormatter(),
        )
    finally:
        COMMANDS["/cancel"] = original

    assert "handler exploded" in host.text


# ── argument coercion + tool metadata ────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"path": "/x.py"}, {"path": "/x.py"}),
        ('{"path": "/x.py"}', {"path": "/x.py"}),
        ("not-json", {}),
        (["list"], {}),
        (None, {}),
    ],
)
def test_coerce_arguments_dict(raw: object, expected: dict[str, object]) -> None:
    assert _coerce_arguments_dict(raw) == expected


def test_tool_metadata_from_flat_and_entity_shapes() -> None:
    flat_name, flat_args = _tool_metadata_from_function_call_item(
        {"name": "Bash", "arguments": {"command": "ls"}}
    )
    assert flat_name == "Bash"
    assert flat_args == {"command": "ls"}

    entity_name, entity_args = _tool_metadata_from_function_call_item(
        {
            "data": {
                "name": "Read",
                "arguments": '{"file_path": "README.md"}',
            }
        }
    )
    assert entity_name == "Read"
    assert entity_args == {"file_path": "README.md"}


def test_build_call_id_to_name_lookup_indexes_function_calls() -> None:
    items = [
        {"type": "message", "role": "user"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "Bash",
            "arguments": {"command": "pwd"},
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "ok",
        },
    ]
    assert _build_call_id_to_name_lookup(items) == {"call_1": "Bash"}


def test_extract_message_text_joins_text_blocks() -> None:
    item = {
        "content": [
            {"type": "input_text", "text": "Hello"},
            {"type": "output_text", "text": "world"},
        ]
    }
    assert _extract_message_text(item) == "Hello world"


def test_extract_function_call_output_text_accepts_nested_data() -> None:
    assert _extract_function_call_output_text({"output": "flat"}) == "flat"
    assert (
        _extract_function_call_output_text({"data": {"output": "nested"}}) == "nested"
    )
    assert _extract_function_call_output_text({}) == ""


# ── overview event renderers ─────────────────────────────────────────────────


def test_render_overview_message_event_user_and_assistant() -> None:
    fmt = RichBlockFormatter()
    user = _plain_list(
        _render_overview_message_event(
            1,
            {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
            fmt,
        )
    )
    assert "type=user_message" in user
    assert "Hi" in user

    assistant = _plain_list(
        _render_overview_message_event(
            2,
            {
                "role": "assistant",
                "model": "claude-sonnet",
                "content": [{"type": "output_text", "text": "Done"}],
            },
            fmt,
        )
    )
    assert "type=assistant_message" in assistant
    assert "model=claude-sonnet" in assistant
    assert "Done" in assistant


def test_render_overview_event_covers_tool_and_reasoning_branches() -> None:
    fmt = RichBlockFormatter()
    lookup = {"call_1": "Bash"}

    tool_req = _plain_list(
        _render_overview_event(
            1,
            {"type": "function_call", "name": "Bash", "arguments": '{"command":"ls"}'},
            lookup,
            fmt,
        )
    )
    assert "type=tool_call_request" in tool_req
    assert "name=Bash" in tool_req

    tool_done = _plain_list(
        _render_overview_event(
            2,
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            lookup,
            fmt,
        )
    )
    assert "type=tool_call_complete" in tool_done
    assert "name=Bash" in tool_done

    reasoning = _plain_list(
        _render_overview_event(
            3,
            {"type": "reasoning", "summary": "thinking"},
            lookup,
            fmt,
        )
    )
    assert "type=reasoning" in reasoning
    assert "thinking" in reasoning

    unknown = _plain_list(
        _render_overview_event(4, {"type": "custom_event"}, lookup, fmt)
    )
    assert "type=custom_event" in unknown

    missing_type = _plain_list(_render_overview_event(5, {}, lookup, fmt))
    assert "type=(unknown)" in missing_type


# ── _build_debug_overview ────────────────────────────────────────────────────


@dataclass
class _OverviewSession:
    model: str = "test-agent"
    session_id: str | None = "conv_main"
    current_response_id: str | None = None


@pytest.mark.asyncio
async def test_build_debug_overview_no_conversation_yet() -> None:
    session = _OverviewSession(session_id=None, current_response_id=None)
    client = MagicMock()

    group = await _build_debug_overview(
        OverlayTarget(key="main", label="main"),
        client=client,
        session=session,  # type: ignore[arg-type]
        agent_name="demo-agent",
        fmt=RichBlockFormatter(),
    )

    text = _plain(group)
    assert "No conversation yet" in text


@pytest.mark.asyncio
async def test_build_debug_overview_lists_items_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Ping"}],
            }
        ]

    monkeypatch.setattr("omnigent.repl._repl._list_all_conversation_items", _fake_list)

    snap = MagicMock(labels={"tier": "cheap"})
    client = MagicMock()
    client.sessions.get = AsyncMock(return_value=snap)

    session = _OverviewSession(session_id="conv_main")
    group = await _build_debug_overview(
        OverlayTarget(key="conv_main", label="main"),
        client=client,
        session=session,  # type: ignore[arg-type]
        agent_name="demo-agent",
        fmt=RichBlockFormatter(),
    )

    text = _plain(group)
    assert "Messages: 1" in text
    assert "tier=cheap" in text
    assert "type=user_message" in text


@pytest.mark.asyncio
async def test_build_debug_overview_items_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failing_list(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("items down")

    monkeypatch.setattr("omnigent.repl._repl._list_all_conversation_items", _failing_list)

    client = MagicMock()
    client.sessions.get = AsyncMock(return_value=MagicMock(labels={}))

    session = _OverviewSession(session_id="conv_main")
    group = await _build_debug_overview(
        OverlayTarget(key="conv_main", label="main"),
        client=client,
        session=session,  # type: ignore[arg-type]
        agent_name="demo-agent",
        fmt=RichBlockFormatter(),
    )

    assert "Failed to fetch conversation items" in _plain(group)


@pytest.mark.asyncio
async def test_build_debug_overview_terminal_target_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.repl._repl import _TerminalInfo

    async def _fake_terminal_overview(*_args: object, **_kwargs: object) -> str:
        return "TERMINAL_OVERVIEW"

    monkeypatch.setattr(
        "omnigent.repl._repl._build_terminal_overview",
        _fake_terminal_overview,
    )

    info = _TerminalInfo(
        name="bash",
        session="s1",
        socket="/tmp/sock",
        target="main",
        conv_id="conv_x",
    )
    target = OverlayTarget(key=_terminal_target_key(info), label="bash:s1")

    group = await _build_debug_overview(
        target,
        client=MagicMock(),
        session=_OverviewSession(),  # type: ignore[arg-type]
        agent_name="demo-agent",
        fmt=RichBlockFormatter(),
    )

    assert group == "TERMINAL_OVERVIEW"