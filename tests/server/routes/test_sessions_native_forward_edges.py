"""Edge tests for native terminal message forward helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities.conversation import Conversation
from omnigent.server.routes.sessions import (
    _extract_claude_native_runner_failure,
    _forward_native_terminal_message,
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


def _message_body() -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}],
        },
    )


def test_extract_claude_native_runner_failure_parses_sse_failed_event() -> None:
    payload = {
        "type": "response.failed",
        "error": {"message": "tmux pane not found"},
    }
    body = f"data: {json.dumps(payload)}\n\n"
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    )

    assert _extract_claude_native_runner_failure(response) == "tmux pane not found"


def test_extract_claude_native_runner_failure_returns_none_for_plain_success() -> None:
    response = httpx.Response(202, json={"queued": True})
    assert _extract_claude_native_runner_failure(response) is None


@pytest.mark.asyncio
async def test_forward_native_terminal_message_raises_on_transport_error() -> None:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("tunnel closed"))

    with pytest.raises(HTTPException) as exc:
        await _forward_native_terminal_message(
            client,
            "conv_native",
            _native_conv(),
            _message_body(),
        )

    assert exc.value.status_code == 502
    assert "delivery failed" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_forward_native_terminal_message_raises_on_non_2xx() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 503
    response.text = "runner busy"
    client.post = AsyncMock(return_value=response)

    with pytest.raises(HTTPException) as exc:
        await _forward_native_terminal_message(
            client,
            "conv_native",
            _native_conv(),
            _message_body(),
        )

    assert exc.value.status_code == 502
    assert "503" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_forward_native_terminal_message_raises_on_response_failed_in_sse() -> None:
    payload = {
        "type": "response.failed",
        "error": {"message": "send-keys rejected"},
    }
    body = f"data: {json.dumps(payload)}\n\n"
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.text = body
    client.post = AsyncMock(return_value=response)

    with pytest.raises(HTTPException) as exc:
        await _forward_native_terminal_message(
            client,
            "conv_native",
            _native_conv(),
            _message_body(),
        )

    assert exc.value.status_code == 502
    assert "send-keys rejected" in str(exc.value.detail)
