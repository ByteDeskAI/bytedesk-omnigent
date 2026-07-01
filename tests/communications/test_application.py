"""Tests for class-backed chat application admission."""

from __future__ import annotations

import pytest

from omnigent.communications import ChatActor, ChatActorKind, PostSessionEventCommand
from omnigent.communications.application import ChatApplicationService
from omnigent.errors import ErrorCode, OmnigentError


def _actor() -> ChatActor:
    return ChatActor(kind=ChatActorKind.USER, user_id="user_1")


def _command(
    event_type: str,
    payload: dict[str, object] | None = None,
    *,
    tool_specs: tuple[dict[str, object], ...] = (),
) -> PostSessionEventCommand:
    return PostSessionEventCommand(
        session_id="conv_1",
        actor=_actor(),
        event_type=event_type,
        payload=payload or {},
        tool_specs=tool_specs,
    )


def test_chat_application_service_rejects_unknown_event_type() -> None:
    service = ChatApplicationService(
        allowed_event_types=frozenset({"message"}),
        payload_validation_exempt_event_types=frozenset(),
        item_payload_validator=lambda *_args: None,
        tool_spec_validator=lambda _tools: None,
    )

    with pytest.raises(OmnigentError) as exc:
        service.admit_post_event(_command("bogus"))

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "Unknown event type: 'bogus'" in str(exc.value)


def test_chat_application_service_validates_item_payloads_and_skips_control_events() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def validate_item(event_type: str, payload: dict[str, object]) -> None:
        calls.append((event_type, payload))

    service = ChatApplicationService(
        allowed_event_types=frozenset({"message", "interrupt"}),
        payload_validation_exempt_event_types=frozenset({"interrupt"}),
        item_payload_validator=validate_item,
        tool_spec_validator=lambda _tools: None,
    )

    service.admit_post_event(_command("interrupt"))
    service.admit_post_event(_command("message", {"role": "user"}))

    assert calls == [("message", {"type": "message", "role": "user"})]


def test_chat_application_service_wraps_payload_validator_errors() -> None:
    def reject_payload(_event_type: str, _payload: dict[str, object]) -> None:
        raise ValueError("missing content")

    service = ChatApplicationService(
        allowed_event_types=frozenset({"message"}),
        payload_validation_exempt_event_types=frozenset(),
        item_payload_validator=reject_payload,
        tool_spec_validator=lambda _tools: None,
    )

    with pytest.raises(OmnigentError) as exc:
        service.admit_post_event(_command("message"))

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "Invalid data payload for event type 'message': missing content" in str(exc.value)


def test_chat_application_service_validates_client_tool_specs() -> None:
    seen: list[list[dict[str, object]]] = []

    def validate_tools(tools: list[dict[str, object]]) -> None:
        seen.append(tools)
        raise ValueError("bad tool")

    service = ChatApplicationService(
        allowed_event_types=frozenset({"message"}),
        payload_validation_exempt_event_types=frozenset(),
        item_payload_validator=lambda *_args: None,
        tool_spec_validator=validate_tools,
    )

    with pytest.raises(OmnigentError) as exc:
        service.admit_post_event(
            _command(
                "message",
                {"role": "user"},
                tool_specs=({"type": "function", "function": {"name": "x"}},),
            )
        )

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert str(exc.value) == "bad tool"
    assert seen == [[{"type": "function", "function": {"name": "x"}}]]
