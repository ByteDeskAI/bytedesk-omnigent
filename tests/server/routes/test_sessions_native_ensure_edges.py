"""Edge tests for native terminal ensure and failure persistence helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    ErrorData,
    NewConversationItem,
)
from omnigent.entities.pagination import PagedList
from omnigent.server.routes.sessions import (
    _NATIVE_TERMINAL_ENSURE_FAILED_CODE,
    _ensure_native_terminal_ready,
    _forward_native_terminal_message,
    _native_terminal_failure_from_runner_response,
    _persist_native_terminal_failure,
)
from omnigent.server.schemas import SessionEventInput


def _claude_native_conv(session_id: str = "conv_native_ensure") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        agent_id="ag_claude",
        labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
    )


def _message_body(text: str = "hello") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


class _NativeFailureStore:
    """Minimal conversation store for native terminal failure helpers."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self.appended: list[tuple[str, list[ConversationItem]]] = []
        self._items: list[ConversationItem] = []

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
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
        self.appended.append((session_id, persisted))
        return persisted

    def list_items(
        self,
        session_id: str,
        *,
        limit: int = 20,
        order: str = "desc",
    ) -> PagedList[ConversationItem]:
        del session_id, limit, order
        return PagedList(
            data=list(self._items),
            first_id=self._items[0].id if self._items else None,
            last_id=self._items[-1].id if self._items else None,
            has_more=False,
        )

    def update_conversation(self, session_id: str, **kwargs: Any) -> None:
        del session_id, kwargs


@pytest.mark.asyncio
async def test_ensure_native_terminal_ready_returns_success_with_policy_notice() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(
        return_value={"policy_hook_disabled_reason": "Codex CLI too old"},
    )
    client.post = AsyncMock(return_value=response)

    outcome = await _ensure_native_terminal_ready(
        client,
        "conv_native_ensure",
        _claude_native_conv(),
    )

    assert outcome.error is None
    assert outcome.policy_notice == "Codex CLI too old"
    client.post.assert_awaited_once()
    call_kwargs = client.post.await_args.kwargs
    assert call_kwargs["json"]["ensure_native_terminal"] is True


@pytest.mark.asyncio
async def test_ensure_native_terminal_ready_returns_transport_error() -> None:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    outcome = await _ensure_native_terminal_ready(
        client,
        "conv_native_ensure",
        _claude_native_conv(),
    )

    assert outcome.error is not None
    assert outcome.error.code == _NATIVE_TERMINAL_ENSURE_FAILED_CODE
    assert "connection refused" in outcome.error.message
    assert outcome.policy_notice is None


@pytest.mark.asyncio
async def test_ensure_native_terminal_ready_returns_definitive_runner_failure() -> None:
    request = httpx.Request("POST", "http://runner/v1/sessions/conv/res/terminals")
    response = httpx.Response(
        503,
        request=request,
        json={
            "error": {
                "code": "tmux_missing",
                "message": "tmux server not running",
            }
        },
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    outcome = await _ensure_native_terminal_ready(
        client,
        "conv_native_ensure",
        _claude_native_conv(),
    )

    assert outcome.error is not None
    assert outcome.error.code == "tmux_missing"
    assert outcome.error.message == "tmux server not running"
    assert outcome.policy_notice is None


def test_native_terminal_failure_from_runner_rejects_partial_error_dict() -> None:
    request = httpx.Request("POST", "http://runner/v1/sessions/conv/res/terminals")
    response = httpx.Response(
        500,
        request=request,
        json={"error": {"code": "cli_missing", "message": ""}},
    )

    error = _native_terminal_failure_from_runner_response(response, display_name="Claude")

    assert error.code == _NATIVE_TERMINAL_ENSURE_FAILED_CODE
    assert "malformed runner response" in error.message


@pytest.mark.asyncio
async def test_persist_native_terminal_failure_records_user_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []
    conv = _claude_native_conv("conv_term_fail")
    store = _NativeFailureStore(conv)
    error = ErrorData(
        source="execution",
        code="tmux_missing",
        message="tmux server not running",
    )

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_native_subagent_terminal_failure",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_input_consumed",
        lambda sid, item: published.append((sid, "consumed")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_error_event",
        lambda sid, err: published.append((sid, f"error:{err.code}")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_terminal_pending",
        lambda sid, pending: published.append((sid, f"pending:{pending}")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda sid, status, detail=None: published.append((sid, f"status:{status}")),
    )

    item_id = await _persist_native_terminal_failure(
        "conv_term_fail",
        conv,
        _message_body(),
        store,  # type: ignore[arg-type]
        error,
        runner_router=None,
        created_by="alice@example.com",
    )

    assert item_id == "item_0"
    assert len(store.appended) == 2
    assert store.appended[0][1][0].type == "message"
    assert store.appended[1][1][0].type == "error"
    assert ("conv_term_fail", "consumed") in published
    assert any(entry[1] == "status:failed" for entry in published)


@pytest.mark.asyncio
async def test_forward_native_terminal_message_succeeds_on_clean_response() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 202
    response.text = ""
    response.headers = {}
    client.post = AsyncMock(return_value=response)

    await _forward_native_terminal_message(
        client,
        "conv_native_ensure",
        _claude_native_conv(),
        _message_body("ping"),
    )

    client.post.assert_awaited_once()
    posted = client.post.await_args.kwargs["json"]
    assert posted["role"] == "user"
    assert posted["agent_id"] == "ag_claude"
