"""Attention-event predicates — parity with ap-web idleTransitions."""

from __future__ import annotations

TERMINAL_STATUSES = frozenset({"idle", "failed"})


def should_notify_turn_end(previous_status: str | None, new_status: str) -> bool:
    """
    True when status transitions running → idle/failed.

    Mirrors ``detectIdleTransitions`` in ap-web.
    """
    return previous_status == "running" and new_status in TERMINAL_STATUSES


def should_notify_new_elicitation(previous_count: int | None, new_count: int) -> bool:
    """
    True when pending elicitation count increased.

    Mirrors ``detectNewElicitations`` in ap-web.
    """
    return previous_count is not None and new_count > previous_count