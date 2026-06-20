"""Sub-agent work status enum + derived terminal/active sets.

Single source of truth for the runner-local ``_SubagentWorkEntry.status``
lifecycle. The enum *values* are byte-identical to the legacy stringly-typed
status strings (``"launching"`` … ``"cancelled"``), so any serialized or
compared status on the wire, in inboxes, or in the parent registry is
unchanged — a ``StrEnum`` member compares equal to its string value.

The ``_TERMINAL`` / ``_ACTIVE`` frozensets are *derived from the enum* so they
can never drift from the old hand-maintained string tuples/frozensets.

Deliberately fastapi-free: ``omnigent/runner/app.py`` (which imports fastapi)
and ``omnigent/runner/tool_dispatch.py`` both depend on this, but it must stay
importable without pulling fastapi onto the runner identity hot path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class SubagentWorkStatus(StrEnum):
    """Lifecycle status of one async ``sys_session_send`` sub-agent dispatch.

    Each member's value is the exact legacy status string, so existing
    serialized/compared statuses remain byte-identical.
    """

    LAUNCHING = "launching"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal subset, as a type-level guard for ``mark_subagent_work_terminal``.
TerminalStatus = Literal[
    SubagentWorkStatus.COMPLETED,
    SubagentWorkStatus.FAILED,
    SubagentWorkStatus.CANCELLED,
]


# Derived from the enum so they cannot drift from the string tuples.
_TERMINAL: frozenset[SubagentWorkStatus] = frozenset(
    {
        SubagentWorkStatus.COMPLETED,
        SubagentWorkStatus.FAILED,
        SubagentWorkStatus.CANCELLED,
    }
)
_ACTIVE: frozenset[SubagentWorkStatus] = frozenset(
    member for member in SubagentWorkStatus if member not in _TERMINAL
)
