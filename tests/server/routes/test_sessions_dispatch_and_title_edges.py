"""Edge tests for dispatch routing and title seeding helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes.sessions import (
    _dispatch_session_event_to_runner,
    _extract_persistent_item_from_sse,
    _seed_missing_title,
)
from omnigent.server.schemas import SessionEventInput


class _TitleStore:
    """Store capturing title updates for seed-missing-title tests."""

    def __init__(self, *, updated: Conversation | None = None) -> None:
        self.updated: list[tuple[str, str]] = []
        self._updated = updated

    def update_conversation(self, session_id: str, **kwargs: object) -> Conversation | None:
        title = kwargs.get("title")
        if isinstance(title, str):
            self.updated.append((session_id, title))
        if self._updated is not None:
            return self._updated
        return Conversation(
            id=session_id,
            created_at=0,
            updated_at=0,
            root_conversation_id=session_id,
            agent_id="ag_test",
            title=title if isinstance(title, str) else None,
        )


def _plain_conv() -> Conversation:
    return Conversation(
        id="conv_plain",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_plain",
        agent_id="ag_plain",
        title=None,
    )


def _user_body(text: str = "Ship the nightly release") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def test_extract_persistent_item_returns_none_on_compaction_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.parse_item_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad compaction")),
    )
    event = {
        "type": "compaction",
        "summary": "broken",
        "last_item_id": "item_1",
    }
    assert _extract_persistent_item_from_sse(event) is None


def test_extract_persistent_item_returns_none_on_message_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.parse_item_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("bad message")),
    )
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "agent": "claude-opus-4",
            "content": [{"type": "output_text", "text": "x"}],
        },
    }
    assert _extract_persistent_item_from_sse(event, response_id="resp_bad") is None


def test_extract_persistent_item_returns_none_for_non_dict_item() -> None:
    event = {"type": "response.output_item.done", "item": "not-a-dict"}
    assert _extract_persistent_item_from_sse(event) is None


@pytest.mark.asyncio
async def test_seed_missing_title_noops_when_already_titled() -> None:
    conv = _plain_conv()
    conv.title = "Existing title"
    store = _TitleStore()

    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": "ignored"}],
        store,  # type: ignore[arg-type]
    )

    assert store.updated == []
    assert conv.title == "Existing title"


@pytest.mark.asyncio
async def test_seed_missing_title_persists_synthesized_title() -> None:
    conv = _plain_conv()
    store = _TitleStore(
        updated=Conversation(
            id="conv_plain",
            created_at=0,
            updated_at=1,
            root_conversation_id="conv_plain",
            agent_id="ag_plain",
            title="Ship the nightly release",
        )
    )

    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": "Ship the nightly release"}],
        store,  # type: ignore[arg-type]
    )

    assert store.updated == [("conv_plain", "Ship the nightly release")]
    assert conv.title == "Ship the nightly release"


@pytest.mark.asyncio
async def test_seed_missing_title_noops_on_unusable_content() -> None:
    conv = _plain_conv()
    store = _TitleStore()

    await _seed_missing_title(conv, [{"type": "input_text", "text": "   "}], store)  # type: ignore[arg-type]

    assert store.updated == []
    assert conv.title is None


@pytest.mark.asyncio
async def test_dispatch_non_native_delegates_to_forward_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forward = AsyncMock(return_value="item_forwarded")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_event_to_runner",
        forward,
    )
    conv = _plain_conv()
    runner = AsyncMock()

    result = await _dispatch_session_event_to_runner(
        "conv_plain",
        conv,
        _user_body(),
        None,  # type: ignore[arg-type]
        runner,
        agent_name="plain-agent",
        file_store=None,
        artifact_store=None,
        created_by="bob@example.com",
    )

    assert result.item_id == "item_forwarded"
    assert result.pending_id is None
    forward.assert_awaited_once()
    assert forward.await_args.args[0] == "conv_plain"
    assert forward.await_args.kwargs["created_by"] == "bob@example.com"
