"""Edge tests for persist-and-forward runner event helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.entities.conversation import ConversationItem, NewConversationItem
from omnigent.server.routes.sessions import _forward_event_to_runner
from omnigent.server.schemas import SessionEventInput


class _ForwardEventStore:
    """Minimal conversation store for forward-event tests."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self._items: list[ConversationItem] = []

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        del session_id
        persisted = [
            ConversationItem(
                id=f"item_{len(self._items)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
                created_by=item.created_by,
            )
            for item in items
        ]
        self._items.extend(persisted)
        return persisted

    def update_conversation(self, session_id: str, **kwargs: Any) -> None:
        del session_id
        if "title" in kwargs:
            self._conv.title = kwargs["title"]


def _user_message_body(text: str = "ship it") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def _conv(session_id: str = "conv_forward") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        agent_id="ag_forward",
        model_override="claude-opus-4",
        harness_override="claude-sdk",
    )


@pytest.mark.asyncio
async def test_forward_event_persists_and_posts_runner_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []
    conv = _conv()
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=202))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_input_consumed",
        lambda sid, item: published.append((sid, f"consumed:{item.id}")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda *_args, **_kwargs: published.append(("status", "idle")),
    )

    item_id = await _forward_event_to_runner(
        "conv_forward",
        conv,
        _user_message_body(),
        store,  # type: ignore[arg-type]
        client,
        agent_name="research-agent",
        has_mcp_servers=True,
        created_by="alice@example.com",
    )

    assert item_id == "item_0"
    client.post.assert_awaited_once()
    posted = client.post.await_args.kwargs["json"]
    assert posted["type"] == "message"
    assert posted["role"] == "user"
    assert posted["agent_id"] == "ag_forward"
    assert posted["model"] == "research-agent"
    assert posted["has_mcp_servers"] is True
    assert posted["model_override"] == "claude-opus-4"
    assert posted["harness_override"] == "claude-sdk"
    assert posted["persisted_item_id"] == "item_0"
    assert ("conv_forward", "consumed:item_0") in published
    assert ("status", "idle") not in published


@pytest.mark.asyncio
async def test_forward_event_publishes_idle_when_runner_post_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []
    conv = _conv("conv_forward_fail")
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("tunnel down"))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_input_consumed",
        lambda *_args, **_kwargs: published.append("consumed"),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda _sid, status: published.append(status),
    )

    item_id = await _forward_event_to_runner(
        "conv_forward_fail",
        conv,
        _user_message_body(),
        store,  # type: ignore[arg-type]
        client,
    )

    assert item_id == "item_0"
    assert "consumed" not in published
    assert published == ["idle"]


@pytest.mark.asyncio
async def test_forward_event_includes_client_tools_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = _conv("conv_tools")
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=202))
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "use read"}],
        },
        tools=[{"type": "function", "name": "Read", "parameters": {}}],
    )

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_input_consumed", lambda *_: None)

    await _forward_event_to_runner(
        "conv_tools",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["tools"] == body.tools


@pytest.mark.asyncio
async def test_forward_event_includes_extension_instruction_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = _conv("conv_instruction_fragments")
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=202))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_input_consumed", lambda *_: None)
    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_instruction_fragments",
        lambda *, agent_id, spec: [
            f"Organization instructions for {agent_id}",
            f"Agent name seen by extension: {spec.name}",
        ],
    )

    await _forward_event_to_runner(
        "conv_instruction_fragments",
        conv,
        _user_message_body(),
        store,  # type: ignore[arg-type]
        client,
        agent_name="research-agent",
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["instruction_fragments"] == [
        "Organization instructions for ag_forward",
        "Agent name seen by extension: research-agent",
    ]


@pytest.mark.asyncio
async def test_forward_event_swallows_file_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = [{"type": "input_file", "file_id": "file_missing"}]
    conv = _conv("conv_resolve_fail")
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=202))
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": original,
        },
    )

    def _raise(_content, _fs, _as, session_id):
        raise ValueError("artifact missing")

    monkeypatch.setattr(
        "omnigent.runtime.content_resolver._resolve_message_content",
        _raise,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_input_consumed", lambda *_: None)

    await _forward_event_to_runner(
        "conv_resolve_fail",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,
        file_store=MagicMock(),
        artifact_store=MagicMock(),
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["content"] == original


@pytest.mark.asyncio
async def test_forward_event_resolves_file_refs_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = [{"type": "input_image", "image_url": "data:image/png;base64,abc"}]
    conv = _conv("conv_resolve_ok")
    store = _ForwardEventStore(conv)
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=202))
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_file", "file_id": "file_1"}],
        },
    )

    monkeypatch.setattr(
        "omnigent.runtime.content_resolver._resolve_message_content",
        lambda content, _fs, _as, session_id: resolved,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_input_consumed", lambda *_: None)

    await _forward_event_to_runner(
        "conv_resolve_ok",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,
        file_store=MagicMock(),
        artifact_store=MagicMock(),
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["content"] == resolved
