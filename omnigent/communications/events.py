"""Internal domain events for chat and delegation orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from omnigent.communications.state import SessionStatus


@dataclass(frozen=True, slots=True)
class SessionStatusChanged:
    """A session lifecycle status changed."""

    session_id: str
    status: SessionStatus
    previous_status: SessionStatus | None = None
    response_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class InputAccepted:
    """A session input was accepted and can be reflected in live projections."""

    session_id: str
    item_id: str
    pending_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChildSessionUpdated:
    """A parent-visible child session summary changed."""

    parent_session_id: str
    child_session_id: str
    status: SessionStatus | None = None
    title: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BlueprintNodeUpdated:
    """A blueprint node made progress or reached a terminal state."""

    session_id: str
    node_id: str
    state: str
    output: Mapping[str, object] = field(default_factory=dict)


__all__ = [
    "BlueprintNodeUpdated",
    "ChildSessionUpdated",
    "InputAccepted",
    "SessionStatusChanged",
]
