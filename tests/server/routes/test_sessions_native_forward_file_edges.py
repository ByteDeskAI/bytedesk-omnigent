"""Edge tests for native terminal forward file-resolution and SSE parse gaps."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import (
    _extract_claude_native_runner_failure,
    _forward_native_terminal_message,
    _parse_skill_slash_command,
    _resolve_skill_meta_text_via_runner,
)
from omnigent.server.schemas import SessionEventInput


def _native_conv() -> Conversation:
    return Conversation(
        id="conv_native_file",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_native_file",
        agent_id="ag_native",
        labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
    )


def _message_body_with_file() -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "file_id": "file_abc",
                }
            ],
        },
    )


def test_parse_skill_slash_command_rejects_empty_name() -> None:
    body = SessionEventInput(
        type="slash_command",
        data={"kind": "skill", "name": "   ", "arguments": "go"},
    )
    with pytest.raises(OmnigentError) as exc:
        _parse_skill_slash_command(body)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "data.name" in str(exc.value)


def test_parse_skill_slash_command_rejects_non_string_arguments() -> None:
    body = SessionEventInput(
        type="slash_command",
        data={"kind": "skill", "name": "lint", "arguments": 42},
    )
    with pytest.raises(OmnigentError) as exc:
        _parse_skill_slash_command(body)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "arguments must be a string" in str(exc.value)


def test_extract_claude_native_runner_failure_skips_invalid_json_frame() -> None:
    failed_payload = {"type": "response.failed", "error": {"message": "late failure"}}
    frames = [
        "data: not-json\n\n",
        f"data: {json.dumps(failed_payload)}\n\n",
    ]
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(frames),
    )
    assert _extract_claude_native_runner_failure(response) == "late failure"


@pytest.mark.asyncio
async def test_resolve_skill_meta_text_raises_on_runner_http_error() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    try:
        with pytest.raises(OmnigentError) as exc:
            await _resolve_skill_meta_text_via_runner("conv_skill", "lint", "", client)
    finally:
        await client.aclose()

    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "HTTP 500" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_skill_meta_text_raises_when_meta_text_missing() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://runner")
    try:
        with pytest.raises(OmnigentError) as exc:
            await _resolve_skill_meta_text_via_runner("conv_skill", "lint", "", client)
    finally:
        await client.aclose()

    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "meta_text" in str(exc.value)


@pytest.mark.asyncio
async def test_forward_native_terminal_message_resolves_file_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = [{"type": "input_image", "image_url": "data:image/png;base64,abc"}]
    monkeypatch.setattr(
        "omnigent.runtime.content_resolver._resolve_message_content",
        lambda content, _fs, _as, session_id: resolved,
    )
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 202
    response.text = ""
    response.headers = {}
    client.post = AsyncMock(return_value=response)
    file_store = MagicMock()
    artifact_store = MagicMock()

    await _forward_native_terminal_message(
        client,
        "conv_native_file",
        _native_conv(),
        _message_body_with_file(),
        file_store=file_store,
        artifact_store=artifact_store,
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["content"] == resolved


@pytest.mark.asyncio
async def test_forward_native_terminal_message_swallows_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = [{"type": "input_file", "file_id": "file_abc"}]

    def _raise(_content, _fs, _as, session_id):
        raise KeyError("missing artifact")

    monkeypatch.setattr(
        "omnigent.runtime.content_resolver._resolve_message_content",
        _raise,
    )
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 202
    response.text = ""
    response.headers = {}
    client.post = AsyncMock(return_value=response)

    await _forward_native_terminal_message(
        client,
        "conv_native_file",
        _native_conv(),
        _message_body_with_file(),
        file_store=MagicMock(),
        artifact_store=MagicMock(),
    )

    posted = client.post.await_args.kwargs["json"]
    assert posted["content"] == original
