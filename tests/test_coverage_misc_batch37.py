"""Batch-37 coverage for /history and remaining /model provider branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import _collect_overview_targets, handle_slash_command


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []
        self.rendered_items: list[dict[str, object]] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)


class _StubFmt:
    muted = "dim"
    accent = "cyan"


@dataclass
class _HistorySession:
    model: str = "test-agent"
    session_id: str | None = "conv_hist"
    current_response_id: str | None = None
    model_override: str | None = None
    harness: str | None = "claude-sdk"
    llm_model: str | None = "anthropic/claude-sonnet-4-6"
    is_streaming: bool = False

    async def set_model_override(self, model: str | None) -> None:
        self.model_override = model


def _rendered(host: _CaptureHost) -> str:
    return "\n".join(getattr(o, "plain", str(o)) for o in host.outputs)


@pytest.mark.asyncio
async def test_history_command_renders_sessions_api_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "response_id": "resp_1",
            "content": [{"type": "output_text", "text": "prior turn"}],
        }
    ]
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )

    captured: list[dict[str, object]] = []

    def _capture(item: dict[str, object], host: object, *_args: object, **_kwargs: object) -> None:
        captured.append(item)

    monkeypatch.setattr("omnigent.repl._repl._render_history_item", _capture)

    host = _CaptureHost()
    client = MagicMock()
    await handle_slash_command(
        "/history",
        _HistorySession(),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert captured == items


@pytest.mark.asyncio
async def test_history_command_renders_sessions_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(side_effect=RuntimeError("items unavailable")),
    )

    host = _CaptureHost()
    await handle_slash_command(
        "/history",
        _HistorySession(),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "Error: items unavailable" in _rendered(host)


@pytest.mark.asyncio
async def test_history_command_without_active_conversation() -> None:
    host = _CaptureHost()
    await handle_slash_command(
        "/history",
        _HistorySession(session_id=None, current_response_id=None),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "No active conversation." in _rendered(host)


@pytest.mark.asyncio
async def test_history_command_legacy_path_renders_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "msg_legacy",
            "type": "message",
            "role": "user",
            "status": "completed",
            "response_id": "resp_legacy",
            "content": [{"type": "input_text", "text": "legacy"}],
        }
    ]
    captured: list[dict[str, object]] = []

    def _capture(item: dict[str, object], host: object, *_args: object, **_kwargs: object) -> None:
        captured.append(item)

    client = MagicMock()
    conversation = MagicMock(id="conv_legacy")
    response = MagicMock(conversation=conversation)
    client.responses.get = AsyncMock(return_value=response)

    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )
    monkeypatch.setattr("omnigent.repl._repl._render_history_item", _capture)

    host = _CaptureHost()
    await handle_slash_command(
        "/history",
        _HistorySession(session_id=None, current_response_id="resp_current"),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert captured == items


@pytest.mark.asyncio
async def test_history_command_legacy_path_without_conversation() -> None:
    client = MagicMock()
    response = MagicMock(conversation=None)
    client.responses.get = AsyncMock(return_value=response)

    host = _CaptureHost()
    await handle_slash_command(
        "/history",
        _HistorySession(session_id=None, current_response_id="resp_current"),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "No conversation." in _rendered(host)


@pytest.mark.asyncio
async def test_model_command_subscription_provider_without_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "providers": {
            "claude": {"kind": "subscription", "default": True, "cli": "claude"},
        }
    }
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    host = _CaptureHost()
    await handle_slash_command(
        "/model claude",
        _HistorySession(),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "picks the model itself" in _rendered(host)


@pytest.mark.asyncio
async def test_collect_overview_targets_uses_sessions_api_conversation_id(
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

    session = _HistorySession(session_id="conv_overview")
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    assert len(targets) == 1
    assert targets[0].key == "conv_overview"
    assert targets[0].label == "main"


@pytest.mark.asyncio
async def test_collect_overview_targets_adds_sub_agent_sidebar_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = {
        "conversation_id": "conv_child",
        "agent": "worker",
        "title": "fib",
        "kind": "sub_agent",
    }
    items = [
        {
            "id": "out_1",
            "type": "function_call_output",
            "output": __import__("json").dumps(handle),
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

    session = _HistorySession(session_id="conv_parent")
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    labels = [t.label for t in targets]
    assert "main" in labels
    assert "worker:fib" in labels