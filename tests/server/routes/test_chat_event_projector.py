"""Tests for the class-backed session chat event projector."""

from __future__ import annotations

import pytest

from omnigent.entities import ConversationItem, ErrorData, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions._projector import ChatEventProjector
from omnigent.server.schemas import ErrorDetail, SandboxStatus


def _conversation_item(
    *,
    item_id: str = "msg_1",
    role: str = "user",
    text: str = "hello",
    is_meta: bool = False,
) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=MessageData(
            role=role,
            content=[
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text,
                }
            ],
            is_meta=is_meta,
        ),
        created_by="alice@example.com",
    )


def _projector(
    published: list[tuple[str, dict[str, object]]],
    *,
    status_cache: dict[str, str] | None = None,
    sandbox_cache: dict[str, SandboxStatus] | None = None,
    push_service_factory=None,
) -> ChatEventProjector:
    return ChatEventProjector(
        publish=lambda sid, payload: published.append((sid, payload)),
        status_cache=status_cache if status_cache is not None else {},
        sandbox_status_cache=sandbox_cache if sandbox_cache is not None else {},
        push_service_factory=push_service_factory,
        clock=lambda: 123.9,
    )


def test_projector_consumed_item_projection_matches_session_stream_shape() -> None:
    """User messages project as session.input.consumed and meta messages skip."""
    published: list[tuple[str, dict[str, object]]] = []
    projector = _projector(published)

    projector.publish_input_consumed("conv_x", _conversation_item(is_meta=True))
    assert published == []

    projector.publish_external_conversation_item(
        "conv_x",
        _conversation_item(),
        cleared_pending_id="pending_abc",
    )

    assert len(published) == 1
    sid, payload = published[0]
    assert sid == "conv_x"
    assert payload["type"] == "session.input.consumed"
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["item_id"] == "msg_1"
    assert data["cleared_pending_id"] == "pending_abc"


def test_projector_status_updates_cache_stream_and_push_service() -> None:
    """Status projection owns sticky transitions, cache writes, and push fanout."""
    published: list[tuple[str, dict[str, object]]] = []
    status_cache = {"conv_status": "failed"}
    push_calls: list[tuple[str, str | None, str]] = []

    class FakePushService:
        def on_status_change(
            self,
            session_id: str,
            previous_status: str | None,
            new_status: str,
        ) -> None:
            push_calls.append((session_id, previous_status, new_status))

    projector = _projector(
        published,
        status_cache=status_cache,
        push_service_factory=FakePushService,
    )

    assert projector.publish_status("conv_status", "idle") is False
    assert published == []
    assert status_cache["conv_status"] == "failed"
    assert push_calls == []

    assert (
        projector.publish_status(
            "conv_status",
            "running",
            error=ErrorDetail(code="runner_error", message="boom"),
            response_id="resp_1",
        )
        is True
    )

    assert status_cache["conv_status"] == "running"
    assert push_calls == [("conv_status", "failed", "running")]
    sid, payload = published[-1]
    assert sid == "conv_status"
    assert payload["type"] == "session.status"
    assert payload["status"] == "running"
    assert payload["response_id"] == "resp_1"


def test_projector_validates_external_text_delta_and_interrupted_shape() -> None:
    """Projection keeps external-delta validation and interrupted payload shape."""
    published: list[tuple[str, dict[str, object]]] = []
    projector = _projector(published)

    with pytest.raises(OmnigentError) as exc:
        projector.publish_external_output_text_delta("conv_delta", {"delta": "hi", "index": True})
    assert exc.value.code == ErrorCode.INVALID_INPUT

    projector.publish_external_output_text_delta(
        "conv_delta",
        {"delta": "hi", "message_id": "msg_1", "index": 0, "final": False},
    )
    assert published[-1][1] == {
        "type": "response.output_text.delta",
        "delta": "hi",
        "message_id": "msg_1",
        "index": 0,
        "final": False,
    }

    projector.publish_interrupted("conv_delta")
    interrupted = published[-1][1]
    assert interrupted["type"] == "session.interrupted"
    data = interrupted["data"]
    assert isinstance(data, dict)
    assert data["requested_at"] == 123
    assert "response_id" not in data


def test_projector_sandbox_status_cache_and_error_event() -> None:
    """Sandbox projection owns cache mutation; errors keep the public SSE shape."""
    published: list[tuple[str, dict[str, object]]] = []
    sandbox_cache: dict[str, SandboxStatus] = {}
    projector = _projector(published, sandbox_cache=sandbox_cache)

    projector.publish_sandbox_status("conv_sandbox", "provisioning")
    assert sandbox_cache["conv_sandbox"].stage == "provisioning"

    projector.publish_sandbox_status("conv_sandbox", "ready")
    assert "conv_sandbox" not in sandbox_cache

    projector.publish_error_event(
        "conv_sandbox",
        ErrorData(source="execution", code="runner_error", message="boom"),
    )
    sid, error_payload = published[-1]
    assert sid == "conv_sandbox"
    assert error_payload["type"] == "response.error"
    assert error_payload["source"] == "execution"
    assert error_payload["error"]["code"] == "runner_error"
    assert error_payload["error"]["message"] == "boom"
