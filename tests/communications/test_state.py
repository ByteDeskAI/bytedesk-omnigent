"""Session communication state rules."""

from __future__ import annotations

import pytest

from omnigent.communications import (
    SessionStatus,
    UnknownSessionStatus,
    is_status_transition_allowed,
    parse_session_status,
    should_publish_status,
)


def test_parse_session_status_accepts_wire_strings_and_enum_members() -> None:
    assert parse_session_status("idle") is SessionStatus.IDLE
    assert parse_session_status(SessionStatus.RUNNING) is SessionStatus.RUNNING


def test_parse_session_status_rejects_unknown_values() -> None:
    with pytest.raises(UnknownSessionStatus, match="unknown session status"):
        parse_session_status("parked")


def test_failed_status_is_sticky_against_trailing_idle() -> None:
    assert should_publish_status("failed", "idle") is False
    assert is_status_transition_allowed(SessionStatus.FAILED, SessionStatus.IDLE) is False


def test_real_work_clears_failed_status() -> None:
    assert should_publish_status("failed", "running") is True
    assert should_publish_status("failed", "launching") is True


def test_known_session_lifecycle_transitions_are_allowed() -> None:
    assert is_status_transition_allowed(None, "launching") is True
    assert is_status_transition_allowed("launching", "running") is True
    assert is_status_transition_allowed("running", "waiting") is True
    assert is_status_transition_allowed("waiting", "running") is True
    assert is_status_transition_allowed("running", "idle") is True


def test_should_publish_defers_unknown_next_status_to_schema_validation() -> None:
    assert should_publish_status("failed", "bogus") is True
