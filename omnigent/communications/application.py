"""Class-backed application services for chat admission."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.communications.commands import PostSessionEventCommand
from omnigent.errors import ErrorCode, OmnigentError

ItemPayloadValidator = Callable[[str, dict[str, Any]], object]
ToolSpecValidator = Callable[[list[dict[str, Any]]], object]


@dataclass(frozen=True)
class ChatApplicationService:
    """Own route-boundary admission rules for session chat commands."""

    allowed_event_types: frozenset[str]
    payload_validation_exempt_event_types: frozenset[str]
    item_payload_validator: ItemPayloadValidator
    tool_spec_validator: ToolSpecValidator

    def admit_post_event(self, command: PostSessionEventCommand) -> None:
        """Validate a post-event command before policy or dispatch."""
        event_type = command.event_type
        if event_type not in self.allowed_event_types:
            raise OmnigentError(
                f"Unknown event type: {event_type!r}. "
                f"Allowed types: {sorted(self.allowed_event_types)}",
                code=ErrorCode.INVALID_INPUT,
            )
        if event_type not in self.payload_validation_exempt_event_types:
            try:
                self.item_payload_validator(
                    event_type,
                    {"type": event_type, **dict(command.payload)},
                )
            except (ValueError, TypeError) as exc:
                raise OmnigentError(
                    f"Invalid data payload for event type {event_type!r}: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        if command.tool_specs:
            try:
                self.tool_spec_validator(_copy_tool_specs(command.tool_specs))
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc


def _copy_tool_specs(tool_specs: Iterable[Mapping[str, object]]) -> list[dict[str, Any]]:
    """Copy tool specs into the mutable dict shape expected by route validators."""
    return [dict(spec) for spec in tool_specs]


__all__ = ["ChatApplicationService", "ItemPayloadValidator", "ToolSpecValidator"]
