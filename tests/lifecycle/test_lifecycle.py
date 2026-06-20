"""Tests for the durable-store lifecycle StrEnums + generic state machine
(BDP-2356, ADR-0142)."""

from __future__ import annotations

import pytest

from bytedesk_omnigent.lifecycle import (
    DeliberationLifecycle,
    DeliberationStatus,
    IllegalTransition,
    LifecycleStateMachine,
    PeerMessageKind,
    ScheduleKind,
    Stance,
    StepLifecycle,
    StepStatus,
    WaitKind,
    WaitLifecycle,
    WaitStatus,
    WorkflowLifecycle,
    WorkflowLifecycleStatus,
)


def test_enum_values_match_legacy_wire_strings() -> None:
    """Each member's value is the exact legacy DB/wire string (wire-compat)."""
    assert [s.value for s in WorkflowLifecycleStatus] == [
        "open",
        "assigned",
        "in_progress",
        "blocked",
        "done",
    ]
    assert [s.value for s in WaitStatus] == ["pending", "resolved", "expired"]
    assert [s.value for s in WaitKind] == ["subscribe", "release"]
    assert [s.value for s in StepStatus] == [
        "pending",
        "running",
        "completed",
        "failed",
    ]
    assert [s.value for s in DeliberationStatus] == ["open", "decided"]
    assert [s.value for s in Stance] == ["for", "against", "amend"]
    assert [s.value for s in PeerMessageKind] == ["dm", "broadcast", "escalation"]
    assert [s.value for s in ScheduleKind] == ["interval", "cron", "once"]


def test_strenum_is_str_compatible_round_trip() -> None:
    """A StrEnum member equals its string and coerces back from the raw string."""
    assert WorkflowLifecycleStatus.OPEN == "open"
    assert str(WorkflowLifecycleStatus.DONE) == "done"
    assert WorkflowLifecycleStatus("in_progress") is WorkflowLifecycleStatus.IN_PROGRESS
    # Equal hash so a string-keyed dict lookup still resolves (scheduler relies on this).
    assert {"interval": 1}[ScheduleKind.INTERVAL] == 1


def test_coercing_unknown_string_raises() -> None:
    with pytest.raises(ValueError):
        WorkflowLifecycleStatus("garbage")


def test_state_machine_can_allows_legal_rejects_illegal() -> None:
    sm = WorkflowLifecycle()
    _S = WorkflowLifecycleStatus
    # legal
    assert sm.can(_S.OPEN, _S.ASSIGNED)
    assert sm.can(_S.ASSIGNED, _S.IN_PROGRESS)
    assert sm.can(_S.IN_PROGRESS, _S.BLOCKED)
    assert sm.can(_S.BLOCKED, _S.IN_PROGRESS)
    assert sm.can(_S.IN_PROGRESS, _S.DONE)
    # illegal: done is terminal
    assert not sm.can(_S.DONE, _S.IN_PROGRESS)
    assert not sm.can(_S.DONE, _S.OPEN)


def test_state_machine_check_raises_illegal_transition() -> None:
    sm = WorkflowLifecycle()
    sm.check(WorkflowLifecycleStatus.OPEN, WorkflowLifecycleStatus.DONE)  # legal, no raise
    with pytest.raises(IllegalTransition):
        sm.check(WorkflowLifecycleStatus.DONE, WorkflowLifecycleStatus.IN_PROGRESS)


def test_generic_base_is_parameterizable() -> None:
    """A fresh subclass can declare its own table over any StrEnum."""

    class WaitSM(LifecycleStateMachine[WaitStatus]):
        transitions = {WaitStatus.PENDING: frozenset({WaitStatus.RESOLVED})}

    sm = WaitSM()
    assert sm.can(WaitStatus.PENDING, WaitStatus.RESOLVED)
    assert not sm.can(WaitStatus.RESOLVED, WaitStatus.PENDING)


def test_per_store_tables_terminalize_correctly() -> None:
    assert WaitLifecycle().can(WaitStatus.PENDING, WaitStatus.EXPIRED)
    assert not WaitLifecycle().can(WaitStatus.RESOLVED, WaitStatus.PENDING)
    assert StepLifecycle().can(StepStatus.RUNNING, StepStatus.PENDING)  # retry
    assert not StepLifecycle().can(StepStatus.COMPLETED, StepStatus.RUNNING)
    assert DeliberationLifecycle().can(DeliberationStatus.OPEN, DeliberationStatus.DECIDED)
    assert not DeliberationLifecycle().can(DeliberationStatus.DECIDED, DeliberationStatus.OPEN)
