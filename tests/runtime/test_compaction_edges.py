"""Edge-case coverage for omnigent.runtime.compaction."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.entities import ConversationItem, MessageData
from omnigent.llms.errors import RetryableLLMError
from omnigent.runtime import session_stream
from omnigent.runtime.compaction import (
    _find_last_summarized_item_id,
    _find_recent_boundary,
    _truncate_oldest,
    compact,
    summarize_history,
)
from omnigent.spec.types import CompactionConfig


def _user_item(item_id: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )


def _assistant_item(item_id: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=2,
        data=MessageData(
            role="assistant",
            content=[{"type": "output_text", "text": "ok"}],
            agent="test-model",
        ),
    )


@pytest.fixture(autouse=True)
def _clean_session_stream() -> None:
    session_stream._subscribers.clear()
    session_stream._replay.clear()
    session_stream._seq.clear()
    yield
    session_stream._subscribers.clear()
    session_stream._replay.clear()
    session_stream._seq.clear()


def test_find_recent_boundary_zero_window_protects_nothing() -> None:
    history = [_user_item("u1"), _assistant_item("a1")]
    assert _find_recent_boundary(history, recent_window=0) == len(history)


def test_truncate_oldest_stops_when_drop_count_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 10000,
    )
    monkeypatch.setattr(
        "omnigent.runtime.compaction._pair_aware_drop_count",
        lambda msgs: 0,
    )
    messages = [{"role": "user", "content": "stuck"}]
    assert _truncate_oldest(messages, budget=10, model="test") == messages


@pytest.mark.asyncio
async def test_summarize_history_delegates_to_runner_with_metadata() -> None:
    runner = AsyncMock(spec=httpx.AsyncClient)
    response = httpx.Response(
        200,
        json={"text": "runner summary", "token_count": 12},
        request=httpx.Request("POST", "http://runner/v1/summarize"),
    )
    runner.post = AsyncMock(return_value=response)

    result = await summarize_history(
        [{"role": "user", "content": "prior"}],
        llm_client=MagicMock(),
        model="openai/gpt-4o",
        connection={"api_key": "k"},
        runner_client=runner,
        conversation_id="conv_runner",
    )

    assert result == {"text": "runner summary", "token_count": 12}
    runner.post.assert_awaited_once()
    payload = runner.post.await_args.kwargs["json"]
    assert payload["connection"] == {"api_key": "k"}
    assert payload["session_id"] == "conv_runner"


def test_find_last_summarized_item_id_skips_synthetic_ids() -> None:
    history = [
        ConversationItem(
            id="cmp_user",
            type="message",
            status="completed",
            response_id="r",
            created_at=1,
            data=MessageData(role="user", content=[{"type": "input_text", "text": "x"}]),
        ),
        ConversationItem(
            id="synthetic_0",
            type="message",
            status="completed",
            response_id="r",
            created_at=2,
            data=MessageData(role="assistant", content=[{"type": "output_text", "text": "y"}], agent="m"),
        ),
    ]
    assert _find_last_summarized_item_id(history, history_boundary=2) is None


@pytest.mark.asyncio
async def test_compact_publishes_session_stream_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        call_idx[0] += 1
        if call_idx[0] == 1:
            return 10001
        return 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _stub_summarize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"text": "summary", "token_count": 10}

    monkeypatch.setattr("omnigent.runtime.compaction.summarize_history", _stub_summarize)

    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish",
        lambda conv_id, event: published.append(event),
    )

    history = [_user_item("u1"), _assistant_item("a1"), _user_item("u2"), _assistant_item("a2")]
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]

    await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_stream",
        llm_client=MagicMock(),
        conversation_id="conv_stream",
    )

    types = [e["type"] for e in published]
    assert "response.compaction.in_progress" in types
    assert "response.compaction.completed" in types


@pytest.mark.asyncio
async def test_compact_fail_on_summary_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda msgs, model: 10001,
    )

    async def _raise_retryable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RetryableLLMError("down", code="503")

    monkeypatch.setattr("omnigent.runtime.compaction.summarize_history", _raise_retryable)

    history = [_user_item("u1"), _assistant_item("a1"), _user_item("u2"), _assistant_item("a2")]
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]

    with pytest.raises(RetryableLLMError):
        await compact(
            messages,
            history,
            config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
            context_window=12500,
            system_token_budget=0,
            model="openai/gpt-4o",
            task_id="task_fail",
            llm_client=MagicMock(),
            fail_on_summary_error=True,
        )


@pytest.mark.asyncio
async def test_compact_layer3_publishes_completed_with_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        call_idx[0] += 1
        return 10001 if call_idx[0] <= 2 else 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _raise_retryable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RetryableLLMError("down", code="503")

    monkeypatch.setattr("omnigent.runtime.compaction.summarize_history", _raise_retryable)

    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish",
        lambda conv_id, event: published.append(event),
    )

    history = [_user_item("u1"), _assistant_item("a1"), _user_item("u2"), _assistant_item("a2")]
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]

    await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_l3",
        llm_client=MagicMock(),
        conversation_id="conv_l3",
    )

    assert any(e["type"] == "response.compaction.completed" for e in published)


@pytest.mark.asyncio
async def test_compact_passes_runner_client_to_layer2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        call_idx[0] += 1
        return 10001 if call_idx[0] == 1 else 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    captured: dict[str, Any] = {}

    async def _capture_summarize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"text": "summary", "token_count": 5}

    monkeypatch.setattr("omnigent.runtime.compaction.summarize_history", _capture_summarize)

    runner = AsyncMock(spec=httpx.AsyncClient)
    history = [_user_item("u1"), _assistant_item("a1"), _user_item("u2"), _assistant_item("a2")]
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]

    await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_runner",
        llm_client=MagicMock(),
        runner_client=runner,
        conversation_id="conv_runner_layer2",
    )

    assert captured.get("runner_client") is runner
    assert captured.get("conversation_id") == "conv_runner_layer2"


@pytest.mark.asyncio
async def test_compact_layer2_returns_none_when_only_synthetic_history_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        call_idx[0] += 1
        return 10001 if call_idx[0] <= 2 else 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _stub_summarize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"text": "summary", "token_count": 5}

    monkeypatch.setattr("omnigent.runtime.compaction.summarize_history", _stub_summarize)

    history = [
        ConversationItem(
            id="cmp_user",
            type="message",
            status="completed",
            response_id="r",
            created_at=1,
            data=MessageData(role="user", content=[{"type": "input_text", "text": "old"}]),
        ),
        ConversationItem(
            id="cmp_assistant",
            type="message",
            status="completed",
            response_id="r",
            created_at=2,
            data=MessageData(
                role="assistant",
                content=[{"type": "output_text", "text": "old reply"}],
                agent="test-model",
            ),
        ),
        _assistant_item("a2"),
    ]
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "assistant", "content": "three"},
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_synthetic",
        llm_client=MagicMock(),
    )

    assert result.summary_metadata is None


@pytest.mark.asyncio
async def test_compact_chain_exhausted_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoopLayer:
        async def apply(self, ctx: Any) -> None:
            return None

    monkeypatch.setattr(
        "omnigent.runtime.compaction._default_compaction_chain",
        lambda: [_NoopLayer()],
    )

    with pytest.raises(RuntimeError, match="compaction chain exhausted"):
        await compact(
            [{"role": "user", "content": "x"}],
            [_user_item("u1")],
            config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
            context_window=12500,
            system_token_budget=0,
            model="openai/gpt-4o",
            task_id="task_chain",
            llm_client=MagicMock(),
        )