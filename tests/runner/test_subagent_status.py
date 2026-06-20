"""BDP-2357: SubagentWorkStatus StrEnum + TerminalStatus Literal.

Pins the wire-compat contract (enum values == the legacy status strings),
the enum-derived terminal/active sets, and the typed terminal guard on
``mark_subagent_work_terminal``.
"""

from __future__ import annotations

import pytest

from omnigent.runner.subagent_status import (
    _ACTIVE,
    _TERMINAL,
    SubagentWorkStatus,
)


def test_enum_values_are_byte_identical_to_legacy_strings() -> None:
    """Every member value equals the exact pre-enum status string."""
    assert SubagentWorkStatus.LAUNCHING == "launching"
    assert SubagentWorkStatus.RUNNING == "running"
    assert SubagentWorkStatus.WAITING == "waiting"
    assert SubagentWorkStatus.COMPLETED == "completed"
    assert SubagentWorkStatus.FAILED == "failed"
    assert SubagentWorkStatus.CANCELLED == "cancelled"
    # StrEnum members are str instances, so serialization is unchanged.
    assert {str(m) for m in SubagentWorkStatus} == {
        "launching",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
    }


def test_terminal_set_is_derived_correctly() -> None:
    assert frozenset(
        {
            SubagentWorkStatus.COMPLETED,
            SubagentWorkStatus.FAILED,
            SubagentWorkStatus.CANCELLED,
        }
    ) == _TERMINAL
    assert frozenset({"completed", "failed", "cancelled"}) == _TERMINAL


def test_active_set_is_the_complement_of_terminal() -> None:
    assert frozenset(
        {
            SubagentWorkStatus.LAUNCHING,
            SubagentWorkStatus.RUNNING,
            SubagentWorkStatus.WAITING,
        }
    ) == _ACTIVE
    assert frozenset({"launching", "running", "waiting"}) == _ACTIVE
    # The two sets partition the whole enum — no drift, no overlap.
    assert set(SubagentWorkStatus) == _ACTIVE | _TERMINAL
    assert set() == _ACTIVE & _TERMINAL


def test_mark_terminal_accepts_terminal_statuses() -> None:
    """A terminal status (string or enum) is coerced and accepted."""
    from omnigent.runner import app as runner_app

    runner_app.register_subagent_work(
        parent_session_id="conv_parent_t",
        child_session_id="conv_child_t",
        agent="worker",
        title="t",
    )
    try:
        ack = runner_app.mark_subagent_work_terminal(
            "conv_child_t",
            status=SubagentWorkStatus.COMPLETED,
            output="done",
        )
        assert ack.entry is not None
        assert ack.entry.status == SubagentWorkStatus.COMPLETED
        assert ack.entry.status == "completed"
    finally:
        runner_app.unregister_subagent_work("conv_child_t")


@pytest.mark.parametrize("active", ["launching", "running", "waiting"])
def test_mark_terminal_rejects_active_statuses(active: str) -> None:
    """A non-terminal status raises ValueError via the runtime guard."""
    from omnigent.runner import app as runner_app

    with pytest.raises(ValueError, match="terminal status"):
        runner_app.mark_subagent_work_terminal(
            "conv_child_missing",
            status=active,  # type: ignore[arg-type]
            output=None,
        )


def test_new_work_entry_defaults_to_launching_enum() -> None:
    from omnigent.runner import app as runner_app

    entry = runner_app.register_subagent_work(
        parent_session_id="conv_parent_d",
        child_session_id="conv_child_d",
        agent="worker",
        title="d",
    )
    try:
        assert entry.status is SubagentWorkStatus.LAUNCHING
        assert entry.status == "launching"
    finally:
        runner_app.unregister_subagent_work("conv_child_d")


def test_tool_dispatch_active_check_uses_the_enum() -> None:
    """tool_dispatch imports the enum-derived active set, not a string tuple."""
    from omnigent.runner import tool_dispatch

    assert tool_dispatch._SUBAGENT_ACTIVE_STATUSES is _ACTIVE
    assert tool_dispatch.SubagentWorkStatus is SubagentWorkStatus
