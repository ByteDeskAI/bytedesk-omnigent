"""Result objects returned by chat application services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DispatchDisposition(StrEnum):
    """How a command was handled after admission."""

    ACCEPTED = "accepted"
    FORWARDED = "forwarded"
    QUEUED = "queued"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ChatCommandResult:
    """Common result for start/post chat commands."""

    session_id: str
    disposition: DispatchDisposition
    item_ids: tuple[str, ...] = ()
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class DelegationResult:
    """Result for parent-to-child session delegation."""

    parent_session_id: str
    child_session_id: str
    disposition: DispatchDisposition
    handle_id: str | None = None


__all__ = [
    "ChatCommandResult",
    "DelegationResult",
    "DispatchDisposition",
]
