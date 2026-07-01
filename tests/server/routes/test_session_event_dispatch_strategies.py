"""Tests for class-backed session event dispatch strategies."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import Conversation, ErrorData
from omnigent.server.routes.sessions._runner._dispatch_strategies import (
    DefaultRunnerEventDispatchStrategy,
    NativeTerminalMessageDispatchStrategy,
    SessionEventDispatchContext,
    SessionEventDispatcher,
)
from omnigent.server.schemas import SessionEventInput


@dataclass
class EnsureOutcome:
    """Minimal ensure result used by strategy tests."""

    error: ErrorData | None = None
    policy_notice: str | None = None


class RecordingStrategy:
    """Test strategy that records strategy matching and dispatch order."""

    def __init__(self, name: str, *, matches: bool, item_id: str) -> None:
        self.name = name
        self.matches = matches
        self.item_id = item_id
        self.calls: list[str] = []

    def can_dispatch(self, context: SessionEventDispatchContext) -> bool:
        del context
        self.calls.append(f"{self.name}:can")
        return self.matches

    async def dispatch(self, context: SessionEventDispatchContext):
        del context
        self.calls.append(f"{self.name}:dispatch")
        from omnigent.server.routes.sessions import _SessionEventDispatchResult

        return _SessionEventDispatchResult(item_id=self.item_id, pending_id=None)


def conversation() -> Conversation:
    """Return a minimal conversation row for dispatcher tests."""
    return Conversation(
        id="conv_dispatch",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_dispatch",
        agent_id="ag_dispatch",
    )


def message_body(text: str = "hello") -> SessionEventInput:
    """Return a user message event."""
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def context(body: SessionEventInput | None = None) -> SessionEventDispatchContext:
    """Build a dispatch context with injected test doubles."""
    return SessionEventDispatchContext(
        session_id="conv_dispatch",
        conversation=conversation(),
        body=body or message_body(),
        conversation_store=object(),  # type: ignore[arg-type]
        runner_client=object(),  # type: ignore[arg-type]
        agent_name="agent",
        file_store=None,
        artifact_store=None,
        created_by="alice@example.com",
    )


@pytest.mark.asyncio
async def test_session_event_dispatcher_uses_first_matching_strategy() -> None:
    first = RecordingStrategy("first", matches=False, item_id="item_first")
    second = RecordingStrategy("second", matches=True, item_id="item_second")
    third = RecordingStrategy("third", matches=True, item_id="item_third")

    result = await SessionEventDispatcher((first, second, third)).dispatch(context())

    assert result.item_id == "item_second"
    assert first.calls == ["first:can"]
    assert second.calls == ["second:can", "second:dispatch"]
    assert third.calls == []


@pytest.mark.asyncio
async def test_default_runner_strategy_forwards_context_fields() -> None:
    forward = AsyncMock(return_value="item_forwarded")
    dispatch_context = context()

    result = await DefaultRunnerEventDispatchStrategy(forward_event=forward).dispatch(
        dispatch_context
    )

    assert result.item_id == "item_forwarded"
    assert result.pending_id is None
    forward.assert_awaited_once()
    assert forward.await_args.args[:5] == (
        dispatch_context.session_id,
        dispatch_context.conversation,
        dispatch_context.body,
        dispatch_context.conversation_store,
        dispatch_context.runner_client,
    )
    assert forward.await_args.kwargs == {
        "agent_name": "agent",
        "file_store": None,
        "artifact_store": None,
        "has_mcp_servers": False,
        "created_by": "alice@example.com",
    }


@pytest.mark.asyncio
async def test_native_strategy_rolls_back_pending_input_when_forward_fails() -> None:
    resolved: list[tuple[str, str]] = []

    async def ensure_ready(*_args: object) -> EnsureOutcome:
        return EnsureOutcome()

    async def fail_forward(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("runner rejected message")

    strategy = NativeTerminalMessageDispatchStrategy(
        is_native_terminal_session=lambda _conv: True,
        build_native_terminal_message_event=lambda _conv, _body: {"type": "message"},
        ensure_native_terminal_ready=ensure_ready,
        persist_native_terminal_failure=AsyncMock(),
        persist_native_policy_notice=AsyncMock(),
        record_pending_input=lambda session_id, _content, *, created_by: (
            f"pending_{session_id}_{created_by}"
        ),
        resolve_pending_input=lambda session_id, pending_id: resolved.append(
            (session_id, pending_id)
        ),
        forward_native_terminal_message=fail_forward,
    )

    with pytest.raises(RuntimeError, match="runner rejected message"):
        await strategy.dispatch(context())

    assert resolved == [("conv_dispatch", "pending_conv_dispatch_alice@example.com")]


@pytest.mark.asyncio
async def test_native_strategy_persists_ensure_failure_without_forwarding() -> None:
    error = ErrorData(source="execution", code="native_failed", message="native failed")
    persist_failure = AsyncMock(return_value="item_error")
    forward = AsyncMock()

    async def ensure_failed(*_args: object) -> EnsureOutcome:
        return EnsureOutcome(error=error)

    strategy = NativeTerminalMessageDispatchStrategy(
        is_native_terminal_session=lambda _conv: True,
        build_native_terminal_message_event=lambda _conv, _body: {"type": "message"},
        ensure_native_terminal_ready=ensure_failed,
        persist_native_terminal_failure=persist_failure,
        persist_native_policy_notice=AsyncMock(),
        record_pending_input=lambda *_args, **_kwargs: "pending_never",
        resolve_pending_input=lambda *_args, **_kwargs: None,
        forward_native_terminal_message=forward,
    )

    dispatch_context = context()
    result = await strategy.dispatch(dispatch_context)

    assert result.item_id == "item_error"
    assert result.pending_id is None
    persist_failure.assert_awaited_once_with(
        "conv_dispatch",
        dispatch_context.conversation,
        dispatch_context.body,
        dispatch_context.conversation_store,
        error,
        None,
        created_by="alice@example.com",
    )
    forward.assert_not_awaited()
