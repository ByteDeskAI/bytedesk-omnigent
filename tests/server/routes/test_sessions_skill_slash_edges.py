"""Edge tests for skill slash-command dispatch helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities import Agent, Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import (
    _dispatch_skill_slash_command_to_runner,
    _resolve_skill_meta_text_via_runner,
)
from omnigent.server.schemas import SessionEventInput

pytestmark = pytest.mark.asyncio


def _slash_body(name: str = "grill-me", arguments: str = "review plan") -> SessionEventInput:
    return SessionEventInput(
        type="slash_command",
        data={
            "kind": "skill",
            "name": name,
            "arguments": arguments,
        },
    )


def _conversation(session_id: str = "conv_skill") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        agent_id="ag_skill",
        title=None,
    )


def _agent() -> Agent:
    return Agent(
        id="ag_skill",
        created_at=0,
        name="skill-agent",
        bundle_location="test:///bundle",
    )


class _SkillSlashStore:
    """Store capturing slash-command persistence."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, list[object]]] = []
        self.title_updates: list[tuple[str, str]] = []

    def append(self, session_id: str, items: list[object]) -> list[object]:
        from omnigent.entities.conversation import ConversationItem

        persisted = [
            ConversationItem(
                id=f"item_{len(self.appended)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
                created_by=item.created_by,
            )
            for item in items
        ]
        self.appended.append((session_id, persisted))
        return persisted

    def update_conversation(self, session_id: str, **kwargs: object) -> None:
        title = kwargs.get("title")
        if isinstance(title, str):
            self.title_updates.append((session_id, title))


async def test_resolve_skill_meta_text_returns_meta_text() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {"name": "lint", "arguments": "src/"}
        return httpx.Response(200, json={"meta_text": "<skill>lint body</skill>"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    try:
        meta = await _resolve_skill_meta_text_via_runner(
            "conv_skill",
            "lint",
            "src/",
            client,
        )
    finally:
        await client.aclose()

    assert meta == "<skill>lint body</skill>"


async def test_resolve_skill_meta_text_raises_on_transport_error() -> None:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("offline"))

    with pytest.raises(OmnigentError) as exc:
        await _resolve_skill_meta_text_via_runner("conv_skill", "lint", "", client)

    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "unreachable" in str(exc.value).lower()


async def test_resolve_skill_meta_text_raises_on_404_with_available() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"available": ["lint", "review"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    try:
        with pytest.raises(OmnigentError) as exc:
            await _resolve_skill_meta_text_via_runner("conv_skill", "missing", "", client)
    finally:
        await client.aclose()

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "lint" in str(exc.value)


async def test_resolve_skill_meta_text_raises_on_malformed_body() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    try:
        with pytest.raises(OmnigentError) as exc:
            await _resolve_skill_meta_text_via_runner("conv_skill", "lint", "", client)
    finally:
        await client.aclose()

    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "malformed" in str(exc.value).lower()


async def test_dispatch_skill_slash_command_persists_and_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, object]]] = []
    store = _SkillSlashStore()
    conv = _conversation()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"meta_text": "<skill>hidden</skill>"})
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, payload: published.append((sid, payload)),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda *_args, **_kwargs: None,
    )

    try:
        item_id = await _dispatch_skill_slash_command_to_runner(
            "conv_skill",
            conv,
            _slash_body(),
            store,  # type: ignore[arg-type]
            client,
            agent=_agent(),
            has_mcp_servers=False,
            created_by="alice@example.com",
        )
    finally:
        await client.aclose()

    assert item_id == "item_0"
    assert len(store.appended) == 1
    visible, hidden = store.appended[0][1]
    assert visible.type == "slash_command"
    assert hidden.type == "message"
    assert store.title_updates == [("conv_skill", "/grill-me review plan")]
    assert published and published[0][1]["type"] == "response.output_item.done"


async def test_dispatch_skill_slash_command_forwards_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[dict[str, object]] = []
    store = _SkillSlashStore()
    conv = _conversation("conv_skill_override")
    conv.model_override = "parent-model"
    conv.harness_override = "claude-native"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"meta_text": "<skill>hidden</skill>"})
        forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_status", lambda *_: None)

    body = _slash_body()
    body.model_override = "child-model"

    try:
        await _dispatch_skill_slash_command_to_runner(
            "conv_skill_override",
            conv,
            body,
            store,  # type: ignore[arg-type]
            client,
            agent=_agent(),
            has_mcp_servers=False,
            created_by=None,
        )
    finally:
        await client.aclose()

    assert forwarded
    assert forwarded[0]["model_override"] == "child-model"
    assert forwarded[0]["harness_override"] == "claude-native"


async def test_dispatch_skill_slash_command_swallows_runner_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_status: list[str] = []
    store = _SkillSlashStore()
    conv = _conversation("conv_skill_err")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, json={"meta_text": "<skill>hidden</skill>"})
        request = httpx.Request("POST", request.url)
        response = httpx.Response(503, request=request, text="runner down")
        raise httpx.HTTPStatusError("runner down", request=request, response=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda sid, status: published_status.append(status),
    )

    try:
        item_id = await _dispatch_skill_slash_command_to_runner(
            "conv_skill_err",
            conv,
            _slash_body(arguments=""),
            store,  # type: ignore[arg-type]
            client,
            agent=_agent(),
            has_mcp_servers=True,
            created_by=None,
        )
    finally:
        await client.aclose()

    assert item_id == "item_0"
    assert published_status == ["idle"]
