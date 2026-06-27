"""Recurring progress accumulator (BDP-2596 Wave 3, feature 3).

A recurring goal accumulates progress toward a standing metric across fires. Each
fire's outcome/delta adds to ``payload['progress']['current']`` toward a
``target``; the goal never closes (it is a standing recurring goal). The
accumulator lives in the payload (no migration) and is written via the goal
store's atomic ``mutate_payload`` RMW (ADR-0009) so concurrent fires don't lose an
increment.
"""
from __future__ import annotations

from typing import Any

PROGRESS_KEY = "progress"


def accumulate_progress(goal_store, *, goal_id: str, delta: float, now: int | None = None):
    """Add ``delta`` to the goal's progress accumulator (atomic RMW).

    Initializes ``{current: 0, target: None}`` if absent. Does NOT close the goal —
    a recurring goal is standing; progress can exceed ``target`` (the accumulator
    keeps counting). Returns the updated :class:`Goal`.
    """

    def _mutate(payload: dict[str, Any]) -> None:
        progress = payload.get(PROGRESS_KEY)
        if not isinstance(progress, dict):
            progress = {"current": 0, "target": None}
        progress["current"] = (progress.get("current") or 0) + delta
        payload[PROGRESS_KEY] = progress

    return goal_store.mutate_payload(goal_id=goal_id, mutator=_mutate, now=now)


def progress_view(goal) -> dict[str, Any] | None:
    """Current/target/remaining for a goal's accumulator, or ``None`` if it has none.

    ``remaining`` is ``max(0, target - current)`` when a target is set, else
    ``None`` (an open-ended standing metric).
    """
    payload = getattr(goal, "payload", None) or {}
    progress = payload.get(PROGRESS_KEY) if isinstance(payload, dict) else None
    if not isinstance(progress, dict):
        return None
    current = progress.get("current") or 0
    target = progress.get("target")
    remaining = max(0, target - current) if isinstance(target, (int, float)) else None
    return {"current": current, "target": target, "remaining": remaining}


__all__ = ["PROGRESS_KEY", "accumulate_progress", "progress_view"]
