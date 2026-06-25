"""Edge tests for session-change forward and runner-failure parsing helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities.conversation import Conversation, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import (
    _build_native_terminal_message_event,
    _extract_claude_native_runner_failure,
    _forward_session_change_to_runner,
    _RunnerForwardResult,
)
from omnigent.server.schemas import SessionEventInput


def _native_conv() -> Conversation:
    return Conversation(
        id="conv_native",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_native",
        agent_id="ag_native",
        labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
    )


def test_build_native_terminal_message_event_rejects_non_user_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.parse_item_data",
        lambda _item_type, _raw: MessageData(
            role="assistant",
            agent="claude-native-ui",
            content=[{"type": "output_text", "text": "not injectable"}],
        ),
    )
    with pytest.raises(OmnigentError) as exc:
        _build_native_terminal_message_event(_native_conv(), body)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "accept only user message events" in str(exc.value)


def test_extract_claude_native_runner_failure_reads_detail_field() -> None:
    payload = {"type": "response.failed", "error": {"detail": "pane wedged"}}
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"data: {json.dumps(payload)}\n\n",
    )
    assert _extract_claude_native_runner_failure(response) == "pane wedged"


def test_extract_claude_native_runner_failure_serializes_dict_error_without_message() -> None:
    payload = {"type": "response.failed", "error": {"code": "boom", "retry": False}}
    response = httpx.Response(
        200,
        text=f"data: {json.dumps(payload)}\n\n",
    )
    result = _extract_claude_native_runner_failure(response)
    assert result is not None
    assert "boom" in result


def test_extract_claude_native_runner_failure_returns_string_error() -> None:
    payload = {"type": "response.failed", "error": "hard stop"}
    response = httpx.Response(200, text=f"data: {json.dumps(payload)}\n\n")
    assert _extract_claude_native_runner_failure(response) == "hard stop"


def test_extract_claude_native_runner_failure_defaults_when_error_missing() -> None:
    payload = {"type": "response.failed"}
    response = httpx.Response(200, text=f"data: {json.dumps(payload)}\n\n")
    assert _extract_claude_native_runner_failure(response) == "runner reported response.failed"


@pytest.mark.asyncio
async def test_forward_session_change_returns_none_without_runner_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)

    result = await _forward_session_change_to_runner(
        "conv_none",
        runner_router=None,
        event={"type": "compact"},
    )

    assert result is None


@pytest.mark.asyncio
async def test_forward_session_change_uses_global_fallback_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 204
    response.text = ""
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: client)

    result = await _forward_session_change_to_runner(
        "conv_global",
        runner_router=MagicMock(),
        event={"type": "effort_change", "effort": "high"},
    )

    assert result == _RunnerForwardResult(status_code=204, body="")
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_session_change_returns_none_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=client),
    )

    result = await _forward_session_change_to_runner(
        "conv_down",
        runner_router=MagicMock(),
        event={"type": "model_change", "model": "claude-opus-4-7"},
    )

    assert result is None


@pytest.mark.asyncio
async def test_forward_session_change_returns_non_2xx_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 503
    response.text = "pane not ready"
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=client),
    )

    result = await _forward_session_change_to_runner(
        "conv_503",
        runner_router=MagicMock(),
        event={"type": "compact"},
    )

    assert result == _RunnerForwardResult(status_code=503, body="pane not ready")
