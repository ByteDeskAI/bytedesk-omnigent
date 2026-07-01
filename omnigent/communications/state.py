"""Session communication state vocabulary and transition rules."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class SessionStatus(StrEnum):
    """Canonical session lifecycle statuses used by REST/SSE projections."""

    FAILED = "failed"
    IDLE = "idle"
    LAUNCHING = "launching"
    RUNNING = "running"
    WAITING = "waiting"


class UnknownSessionStatus(ValueError):
    """Raised when a status value is outside the session communication vocabulary."""


class InvalidSessionStatusTransition(ValueError):
    """Raised when a known status transition violates communication invariants."""


_ALL_STATUSES = frozenset(SessionStatus)

_ALLOWED_TRANSITIONS: Mapping[SessionStatus | None, frozenset[SessionStatus]] = {
    None: _ALL_STATUSES,
    SessionStatus.FAILED: frozenset(
        {
            SessionStatus.FAILED,
            SessionStatus.LAUNCHING,
            SessionStatus.RUNNING,
        }
    ),
    SessionStatus.IDLE: frozenset(
        {
            SessionStatus.FAILED,
            SessionStatus.IDLE,
            SessionStatus.LAUNCHING,
            SessionStatus.RUNNING,
        }
    ),
    SessionStatus.LAUNCHING: frozenset(
        {
            SessionStatus.FAILED,
            SessionStatus.IDLE,
            SessionStatus.LAUNCHING,
            SessionStatus.RUNNING,
            SessionStatus.WAITING,
        }
    ),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.FAILED,
            SessionStatus.IDLE,
            SessionStatus.RUNNING,
            SessionStatus.WAITING,
        }
    ),
    SessionStatus.WAITING: frozenset(
        {
            SessionStatus.FAILED,
            SessionStatus.IDLE,
            SessionStatus.RUNNING,
            SessionStatus.WAITING,
        }
    ),
}


def parse_session_status(value: str | SessionStatus) -> SessionStatus:
    """
    Parse a wire status into the internal ``SessionStatus`` vocabulary.

    :param value: Raw status string or an existing enum member.
    :returns: The matching enum member.
    :raises UnknownSessionStatus: when *value* is not a known session status.
    """
    if isinstance(value, SessionStatus):
        return value
    try:
        return SessionStatus(value)
    except ValueError as exc:
        raise UnknownSessionStatus(f"unknown session status: {value!r}") from exc


def is_status_transition_allowed(
    previous_status: str | SessionStatus | None,
    next_status: str | SessionStatus,
) -> bool:
    """
    Return whether a known session status transition is allowed.

    ``None`` means the cache has no prior status, so every known status can be
    published as the first observed edge.
    """
    previous = parse_session_status(previous_status) if previous_status is not None else None
    next_value = parse_session_status(next_status)
    return next_value in _ALLOWED_TRANSITIONS[previous]


def should_publish_status(
    previous_status: str | SessionStatus | None,
    next_status: str | SessionStatus,
) -> bool:
    """
    Return whether the status publisher should emit the requested edge.

    This helper is intentionally permissive for unknown *next_status* values so
    route-level Pydantic validation remains the fail-loud schema gate. For
    known statuses it centralizes the communication invariant that ``failed``
    is sticky against a trailing ``idle`` quiescence signal until real work
    resumes.
    """
    try:
        return is_status_transition_allowed(previous_status, next_status)
    except UnknownSessionStatus:
        return True


__all__ = [
    "InvalidSessionStatusTransition",
    "SessionStatus",
    "UnknownSessionStatus",
    "is_status_transition_allowed",
    "parse_session_status",
    "should_publish_status",
]
