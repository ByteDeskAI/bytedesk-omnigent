"""Batch-38 coverage for context fetch helpers and overview target edges."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import (
    _collect_overview_targets,
    _collect_terminals_for_conversations,
    _fetch_context_items,
    _items_for_context_token_count,
    handle_slash_command,
)


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)


class _StubFmt:
    muted = "dim"
    accent = "cyan"

    def welcome(self, name: str, hints: object) -> str:
        return f"<welcome:{name}>"


@dataclass
class _ContextSession:
    model: str = "test-agent"
    session_id: str | None = "conv_ctx"
    current_response_id: str | None = None
    reset_calls: int = 0

    def reset(self) -> None:
        self.reset_calls += 1


def _rendered(host: _CaptureHost) -> str:
    return "\n".join(getattr(o, "plain", str(o)) for o in host.outputs)


def _terminal_launch_items() -> list[dict[str, object]]:
    return [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "sys_terminal_launch",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "status": "launched",
                }
            ),
        },
    ]


@pytest.mark.asyncio
async def test_fetch_context_items_sessions_api_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [{"id": "msg_1", "type": "message", "role": "user", "content": "hi"}]
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )
    result = await _fetch_context_items(
        _ContextSession(),  # type: ignore[arg-type]
        MagicMock(),
    )
    assert result.error is None
    assert result.items == items


@pytest.mark.asyncio
async def test_fetch_context_items_sessions_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(side_effect=RuntimeError("items down")),
    )
    result = await _fetch_context_items(
        _ContextSession(),  # type: ignore[arg-type]
        MagicMock(),
    )
    assert result.items == []
    assert result.error == "items down"


@pytest.mark.asyncio
async def test_fetch_context_items_legacy_path_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [{"id": "msg_legacy", "type": "message", "role": "user", "content": "legacy"}]
    client = MagicMock()
    conversation = MagicMock(id="conv_legacy")
    client.responses.get = AsyncMock(return_value=MagicMock(conversation=conversation))
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )

    result = await _fetch_context_items(
        _ContextSession(session_id=None, current_response_id="resp_legacy"),  # type: ignore[arg-type]
        client,
    )
    assert result.error is None
    assert result.items == items


@pytest.mark.asyncio
async def test_fetch_context_items_legacy_path_error() -> None:
    client = MagicMock()
    client.responses.get = AsyncMock(side_effect=RuntimeError("responses down"))

    result = await _fetch_context_items(
        _ContextSession(session_id=None, current_response_id="resp_legacy"),  # type: ignore[arg-type]
        client,
    )
    assert result.items == []
    assert result.error == "responses down"


@pytest.mark.asyncio
async def test_fetch_context_items_returns_empty_when_no_conversation() -> None:
    result = await _fetch_context_items(
        _ContextSession(session_id=None, current_response_id=None),  # type: ignore[arg-type]
        MagicMock(),
    )
    assert result.items == []
    assert result.error is None


def test_items_for_context_token_count_filters_resource_events() -> None:
    items = [
        {"id": "evt_1", "type": "resource_event", "role": "system"},
        {"id": "msg_1", "type": "message", "role": "user", "content": "visible"},
    ]
    effective = _items_for_context_token_count(items)
    assert [item.get("id") for item in effective] == ["msg_1"]


def test_items_for_context_token_count_malformed_compaction_falls_back() -> None:
    items = [
        {"id": "msg_1", "type": "message", "role": "user", "content": "old"},
        {"id": "cmp_1", "type": "compaction", "summary": 123, "last_item_id": "msg_1"},
        {"id": "msg_2", "type": "message", "role": "user", "content": "new"},
    ]
    effective = _items_for_context_token_count(items)
    ids = [item.get("id") for item in effective]
    assert "cmp_1" not in ids
    assert "msg_1" in ids
    assert "msg_2" in ids


def test_items_for_context_token_count_compaction_with_missing_boundary_uses_summary_only() -> None:
    items = [
        {
            "id": "cmp_1",
            "type": "compaction",
            "summary": "compressed",
            "last_item_id": "missing_item",
        },
        {"id": "msg_2", "type": "message", "role": "user", "content": "recent only"},
    ]
    effective = _items_for_context_token_count(items)
    assert all(item.get("id") != "msg_2" for item in effective)
    assert any(item.get("content") == "compressed" for item in effective)


@pytest.mark.asyncio
async def test_history_command_legacy_path_renders_error() -> None:
    client = MagicMock()
    client.responses.get = AsyncMock(side_effect=RuntimeError("history down"))

    host = _CaptureHost()
    await handle_slash_command(
        "/history",
        _ContextSession(session_id=None, current_response_id="resp_hist"),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "Error: history down" in _rendered(host)


@pytest.mark.asyncio
async def test_new_command_renders_error_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingSession(_ContextSession):
        async def start_new_conversation(self) -> None:
            raise RuntimeError("unbind failed")

    host = _CaptureHost()
    await handle_slash_command(
        "/new",
        _FailingSession(),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    rendered = _rendered(host)
    assert "New conversation failed" in rendered
    assert "unbind failed" in rendered
    assert "<welcome:" not in rendered


@pytest.mark.asyncio
async def test_collect_overview_targets_legacy_path_uses_response_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "omnigent.repl._repl._collect_terminals_for_conversations",
        AsyncMock(return_value=[]),
    )

    client = MagicMock()
    conversation = MagicMock(id="conv_from_response")
    client.responses.get = AsyncMock(return_value=MagicMock(conversation=conversation))

    session = _ContextSession(session_id=None, current_response_id="resp_overview")
    targets = await _collect_overview_targets(client, session)  # type: ignore[arg-type]

    assert targets[0].key == "conv_from_response"


@pytest.mark.asyncio
async def test_collect_overview_targets_returns_main_when_response_lookup_fails() -> None:
    client = MagicMock()
    client.responses.get = AsyncMock(side_effect=RuntimeError("lookup failed"))

    session = _ContextSession(session_id=None, current_response_id="resp_overview")
    targets = await _collect_overview_targets(client, session)  # type: ignore[arg-type]

    assert len(targets) == 1
    assert targets[0].key == "main"


@pytest.mark.asyncio
async def test_collect_overview_targets_skips_malformed_sub_agent_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "out_bad",
            "type": "function_call_output",
            "output": json.dumps({"conversation_id": "conv_child", "agent": "worker"}),
        }
    ]
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )
    monkeypatch.setattr(
        "omnigent.repl._repl._collect_terminals_for_conversations",
        AsyncMock(return_value=[]),
    )

    session = _ContextSession(session_id="conv_parent")
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    assert [t.label for t in targets] == ["main"]


@pytest.mark.asyncio
async def test_collect_overview_targets_adds_terminal_sidebar_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = _terminal_launch_items()
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )

    session = _ContextSession(session_id="conv_term")
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    labels = [t.label for t in targets]
    assert "bash:s1" in labels
    assert any(t.icon == "💻" for t in targets)


@pytest.mark.asyncio
async def test_collect_terminals_for_conversations_uses_seed_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr("omnigent.repl._repl._list_all_conversation_items", fetch)

    items = _terminal_launch_items()
    terminals = await _collect_terminals_for_conversations(
        MagicMock(),
        ["conv_seed"],
        seed_items={"conv_seed": items},
    )

    fetch.assert_not_awaited()
    assert len(terminals) == 1
    assert terminals[0].name == "bash"
    assert terminals[0].session == "s1"
    assert terminals[0].conv_id == "conv_seed"