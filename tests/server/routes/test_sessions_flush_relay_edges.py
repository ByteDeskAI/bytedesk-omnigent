"""Unit tests for ``_flush_relay_text`` relay buffer persistence."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities.conversation import ConversationItem, MessageData, NewConversationItem
from omnigent.server.routes.sessions import _flush_relay_text


class _FlushRelayStore:
    """Minimal store that records append calls for flush-relay tests."""

    def __init__(self, *, raise_on_append: bool = False) -> None:
        self.raise_on_append = raise_on_append
        self.appended: list[tuple[str, list[NewConversationItem]]] = []
        self._items: list[ConversationItem] = []

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        if self.raise_on_append:
            raise RuntimeError("store offline")
        persisted = [
            ConversationItem(
                id=f"item_{len(self._items)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
            )
            for item in items
        ]
        self._items.extend(persisted)
        self.appended.append((session_id, items))
        return persisted


@pytest.mark.asyncio
async def test_flush_relay_text_noops_on_empty_buffer() -> None:
    acc: list[str] = []
    store = _FlushRelayStore()
    await _flush_relay_text(store, "conv_flush", acc, "resp_1", "debby")  # type: ignore[arg-type]
    assert acc == []
    assert store.appended == []


@pytest.mark.asyncio
async def test_flush_relay_text_drops_whitespace_only_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acc = ["   ", "\n"]
    store = _FlushRelayStore()
    reset_calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.inflight_text.reset_text",
        lambda sid: reset_calls.append(sid),
    )
    await _flush_relay_text(store, "conv_ws", acc, "resp_1", "debby")  # type: ignore[arg-type]
    assert acc == []
    assert store.appended == []
    assert reset_calls == ["conv_ws"]


@pytest.mark.asyncio
async def test_flush_relay_text_skips_persist_when_store_none() -> None:
    acc = ["hello"]
    await _flush_relay_text(None, "conv_none", acc, "resp_1", "debby")
    assert acc == []


@pytest.mark.asyncio
async def test_flush_relay_text_persists_and_publishes_output_item_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    store = _FlushRelayStore()
    acc = ["Hello ", "world."]
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.inflight_text.reset_text",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _sid, payload: published.append(payload),
    )

    await _flush_relay_text(store, "conv_ok", acc, "resp_ok", "debby")  # type: ignore[arg-type]

    assert acc == []
    assert len(store.appended) == 1
    persisted = store.appended[0][1][0]
    assert persisted.type == "message"
    assert isinstance(persisted.data, MessageData)
    assert persisted.data.role == "assistant"
    assert persisted.data.content == [{"type": "output_text", "text": "Hello world."}]
    assert len(published) == 1
    assert published[0]["type"] == "response.output_item.done"
    assert published[0]["item"]["content"] == [{"type": "output_text", "text": "Hello world."}]


@pytest.mark.asyncio
async def test_flush_relay_text_keeps_buffer_on_append_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FlushRelayStore(raise_on_append=True)
    acc = ["segment text"]
    reset_calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.inflight_text.reset_text",
        lambda sid: reset_calls.append(sid),
    )
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _sid, payload: published.append(payload),
    )

    await _flush_relay_text(store, "conv_fail", acc, "resp_fail", "debby")  # type: ignore[arg-type]

    assert acc == ["segment text"]
    assert reset_calls == []
    assert published == []
