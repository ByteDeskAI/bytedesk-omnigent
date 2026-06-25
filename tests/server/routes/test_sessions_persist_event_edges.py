"""Edge tests for persist-only session event helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import Conversation
from omnigent.entities.conversation import ConversationItem, NewConversationItem
from omnigent.server.routes.sessions import _persist_session_event
from omnigent.server.schemas import SessionEventInput


class _PersistOnlyStore:
    """Minimal store for ``_persist_session_event`` tests."""

    def __init__(
        self,
        *,
        conv: Conversation | None = None,
        append_returns: list[ConversationItem] | None = None,
    ) -> None:
        self._conv = conv
        self._append_returns = append_returns
        self.appended: list[tuple[str, list[NewConversationItem]]] = []
        self.title_seeded = False

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        self.appended.append((session_id, items))
        if self._append_returns is not None:
            return self._append_returns
        return [
            ConversationItem(
                id="item_persist_0",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
            )
            for item in items
        ]

    def get_conversation(self, session_id: str) -> Conversation | None:
        del session_id
        return self._conv


def _user_body(text: str = "queue for recovery") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def _plain_conv(session_id: str = "conv_persist") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        agent_id="ag_persist",
        title=None,
    )


@pytest.mark.asyncio
async def test_persist_session_event_returns_store_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[Any] = []
    store = _PersistOnlyStore(conv=_plain_conv())
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_external_conversation_item",
        lambda _sid, item, **kwargs: published.append(item),
    )

    item_id = await _persist_session_event(
        "conv_persist",
        _user_body(),
        store,  # type: ignore[arg-type]
    )

    assert item_id == "item_persist_0"
    assert len(store.appended) == 1
    assert store.appended[0][0] == "conv_persist"
    assert published and published[0].id == "item_persist_0"


@pytest.mark.asyncio
async def test_persist_session_event_seeds_title_when_conversation_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_mock = AsyncMock()
    store = _PersistOnlyStore(conv=_plain_conv())
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        seed_mock,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_external_conversation_item",
        lambda *_args, **_kwargs: None,
    )

    await _persist_session_event(
        "conv_persist",
        _user_body("Ship nightly release"),
        store,  # type: ignore[arg-type]
    )

    seed_mock.assert_awaited_once()
    seeded_conv, _seeded_item, seeded_store = seed_mock.await_args.args
    assert seeded_conv.id == "conv_persist"
    assert seeded_store is store


@pytest.mark.asyncio
async def test_persist_session_event_skips_title_seed_when_conversation_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_mock = AsyncMock()
    store = _PersistOnlyStore(conv=None)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        seed_mock,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_external_conversation_item",
        lambda *_args, **_kwargs: None,
    )

    await _persist_session_event(
        "conv_missing",
        _user_body(),
        store,  # type: ignore[arg-type]
    )

    seed_mock.assert_not_awaited()
