"""Batch-36 coverage for _attach_to_conversation and remaining /model paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import (
    WELCOME_HINTS,
    _attach_to_conversation,
    _build_model_readout_lines,
    _resolve_provider_default_model,
    handle_slash_command,
)


def _anthropic_key_provider(*, default: bool = True) -> dict[str, object]:
    entry: dict[str, object] = {
        "kind": "key",
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "api_key_ref": "env:ANTHROPIC_API_KEY",
            "models": {"default": "claude-sonnet-4-6"},
        },
    }
    if default:
        entry["default"] = True
    return entry


class _CaptureHost:
    def __init__(self) -> None:
        self.outputs: list[Any] = []
        self.context_updates: list[tuple[int, int]] = []

    def output(self, item: Any, **_kwargs: object) -> None:
        self.outputs.append(item)

    def update_context_usage(self, tokens: int, context_window: int) -> None:
        self.context_updates.append((tokens, context_window))


class _StubFmt:
    muted = "dim"
    accent = "cyan"

    def welcome(self, name: str, hints: object) -> str:
        return f"<welcome:{name}>"


class _LegacyAttachSession:
    """Session stub without ``session_id`` (legacy resume startup path)."""

    model: str = "test-agent"
    llm_model: str = "anthropic/claude-sonnet-4-6"
    context_window: int = 200_000
    _last_total_tokens: int | None = 5_000
    reset_calls: int = 0
    resume_calls: list[str] = []
    ensure_calls: int = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def resume_from_response(self, response_id: str) -> None:
        self.resume_calls.append(response_id)

    async def _ensure_session(self) -> None:
        self.ensure_calls += 1


@dataclass
class _AttachSession:
    model: str = "test-agent"
    session_id: str = "conv_current"
    llm_model: str = "anthropic/claude-sonnet-4-6"
    context_window: int = 200_000
    _last_total_tokens: int | None = 5_000
    reset_calls: int = 0
    resume_calls: list[str] = field(default_factory=list)
    ensure_calls: int = 0
    model_override: str | None = None
    is_streaming: bool = False

    def reset(self) -> None:
        self.reset_calls += 1

    def resume_from_response(self, response_id: str) -> None:
        self.resume_calls.append(response_id)

    async def _ensure_session(self) -> None:
        self.ensure_calls += 1

    async def set_model_override(self, model: str | None) -> None:
        if model is not None and not str(model).strip():
            raise ValueError("model must not be empty")
        self.model_override = model


def _rendered(host: _CaptureHost) -> str:
    return "\n".join(getattr(o, "plain", str(o)) for o in host.outputs)


@pytest.mark.asyncio
async def test_attach_to_conversation_empty_with_redraw_renders_welcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_calls: list[None] = []
    monkeypatch.setattr(
        "omnigent.repl._repl._clear_screen",
        lambda: clear_calls.append(None),
    )
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=[]),
    )

    host = _CaptureHost()
    session = _AttachSession()
    client = MagicMock()
    client.sessions.get = AsyncMock()

    await _attach_to_conversation(
        "conv_empty",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
        ui_name="Test Agent",
        redraw_screen=True,
    )

    client.sessions.get.assert_awaited_once_with("conv_empty")
    assert session.ensure_calls == 1
    assert clear_calls == [None]
    assert host.outputs == ["<welcome:Test Agent>"]
    assert session.reset_calls == 0


@pytest.mark.asyncio
async def test_attach_to_conversation_empty_without_redraw_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=[]),
    )

    host = _CaptureHost()
    session = _LegacyAttachSession()
    client = MagicMock()

    await _attach_to_conversation(
        "conv_empty",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
        ui_name="Test Agent",
        redraw_screen=False,
    )

    client.sessions.get.assert_not_called()
    assert host.outputs == []


@pytest.mark.asyncio
async def test_attach_to_conversation_resumes_and_renders_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "response_id": "resp_1",
            "content": [{"type": "output_text", "text": "hello from history"}],
        }
    ]
    rendered: list[dict[str, object]] = []

    def _capture_render(item: dict[str, object], host: object, *_args: object, **_kwargs: object) -> None:
        rendered.append(item)

    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )
    monkeypatch.setattr("omnigent.repl._repl._render_history_item", _capture_render)
    monkeypatch.setattr("omnigent.repl._repl._clear_screen", lambda: None)

    host = _CaptureHost()
    session = _AttachSession(_last_total_tokens=12_345)
    client = MagicMock()
    client.sessions.get = AsyncMock()

    await _attach_to_conversation(
        "conv_resume_abcdefghijklmnop",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
        ui_name="Resume Agent",
        redraw_screen=True,
    )

    assert session.reset_calls == 1
    assert session.resume_calls == ["resp_1"]
    assert rendered == items
    assert "Resumed conversation conv_resume_abcd" in _rendered(host)
    assert host.context_updates == [(12_345, 200_000)]


@pytest.mark.asyncio
async def test_attach_to_conversation_counts_tokens_when_no_last_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "status": "completed",
            "response_id": "resp_1",
            "content": [{"type": "input_text", "text": "count me"}],
        }
    ]
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(return_value=items),
    )
    monkeypatch.setattr("omnigent.repl._repl._render_history_item", lambda *_a, **_k: None)
    monkeypatch.setattr("omnigent.repl._repl._clear_screen", lambda: None)
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda _items, _model: 42,
    )

    host = _CaptureHost()
    session = _AttachSession(_last_total_tokens=None)
    client = MagicMock()
    client.sessions.get = AsyncMock()

    await _attach_to_conversation(
        "conv_count",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
        ui_name="Count Agent",
        redraw_screen=False,
    )

    assert host.context_updates == [(42, 200_000)]


def test_build_model_readout_lines_gateway_without_pinned_model() -> None:
    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_ref": "env:OPENROUTER_API_KEY",
                },
            }
        }
    }
    lines = _build_model_readout_lines(config, "claude-sdk", None)
    assert any("no model pinned" in line for line in lines)


def test_resolve_provider_default_model_unknown_provider_returns_none() -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    assert _resolve_provider_default_model(config, "missing") is None


def test_resolve_provider_default_model_subscription_without_model_returns_none() -> None:
    config = {
        "providers": {
            "claude": {"kind": "subscription", "default": True, "cli": "claude"},
        }
    }
    assert _resolve_provider_default_model(config, "claude") is None


@pytest.mark.asyncio
async def test_switch_command_reports_no_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.sessions.list = AsyncMock(return_value=[])

    host = _CaptureHost()
    await handle_slash_command(
        "/switch",
        _AttachSession(),  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "No sessions." in _rendered(host)


@pytest.mark.asyncio
async def test_switch_command_renders_inline_error_on_failure() -> None:
    client = MagicMock()
    client.sessions.list = AsyncMock(return_value=[])

    session = _AttachSession()
    session.switch_to_session = AsyncMock(side_effect=RuntimeError("network down"))  # type: ignore[attr-defined]

    host = _CaptureHost()
    await handle_slash_command(
        "/switch conv_broken",
        session,  # type: ignore[arg-type]
        client,
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "Error: network down" in _rendered(host)


@pytest.mark.asyncio
async def test_model_command_resolves_bare_active_provider_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    session = _AttachSession()
    host = _CaptureHost()
    await handle_slash_command(
        "/model anthropic",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    rendered = _rendered(host)
    assert session.model_override == "claude-sonnet-4-6"
    assert "resolved provider 'anthropic'" in rendered


@pytest.mark.asyncio
async def test_model_command_rejects_invalid_override_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    session = _AttachSession()

    async def _raise_value_error(_model: str | None) -> None:
        raise ValueError("model must not be empty")

    session.set_model_override = _raise_value_error  # type: ignore[method-assign]

    host = _CaptureHost()
    await handle_slash_command(
        "/model anthropic/claude-sonnet-4-6",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    assert "Invalid model: model must not be empty" in _rendered(host)
    assert session.model_override is None


@pytest.mark.asyncio
async def test_model_command_sets_known_model_without_streaming_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"providers": {"anthropic": _anthropic_key_provider()}}
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    monkeypatch.setattr(
        "omnigent.onboarding.detected.effective_config_with_detected",
        lambda cfg: cfg,
    )

    session = _AttachSession(is_streaming=False)
    host = _CaptureHost()
    await handle_slash_command(
        "/model anthropic/claude-sonnet-4-6",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )
    rendered = _rendered(host)
    assert session.model_override == "anthropic/claude-sonnet-4-6"
    assert "model set to anthropic/claude-sonnet-4-6" in rendered
    assert "current response unchanged" not in rendered