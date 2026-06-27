"""Auto-decomposition — split a parent goal into a child-goal tree (BDP-2596, 5).

An org/dept goal is broken into child goals linked by ``parent_goal_id`` with
inherited constraints (budget scope = ``tier``/``target_id``, ``risk_tier``,
deadline). The treasury already gates a child by every ancestor's budget cap
(``_budget_chain``) and the tick already rolls child realized value up to the
parent (``_roll_up_child_outcomes``) — so wiring the tree IS the inheritance.

The decomposition *spec* (the list of children + their overrides) comes from the
planner/chief-of-staff agent or a deterministic split; this module is the
engine-side wiring that turns a spec into linked goals. Per-child fields default
to the parent's (inheritance) and any explicit spec field wins.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Parent fields a child inherits unless the spec overrides them. The budget scope
# (tier/target) is the load-bearing one — it puts the child under the parent's cap.
_INHERITED = (
    "target_kind",
    "target_id",
    "target_label",
    "tier",
    "risk_tier",
)
# Spec keys forwarded to create_goal (title is required; the rest optional).
_CHILD_KEYS = (
    "title",
    "priority",
    "source",
    "payload",
    "target_kind",
    "target_id",
    "target_label",
    "readiness_kind",
    "tier",
    "risk_tier",
    "expected_value_cents",
    "confidence",
    "success_condition",
    "dependencies",
)


def decompose_goal(
    goal_store,
    *,
    parent_goal_id: str,
    spec: Sequence[dict[str, Any]],
    now: int | None = None,
) -> list[Any]:
    """Create child goals under ``parent_goal_id`` from ``spec`` with inheritance.

    Each spec entry needs a ``title``; every other field defaults to the parent's
    (``_INHERITED``) and an explicit entry value overrides it. Children are created
    ``open`` (unowned) so the normal assignment/dispatch path picks them up. Raises
    ``ValueError`` if the parent does not exist. An empty spec is a no-op.
    """
    parent = goal_store.get_goal(goal_id=parent_goal_id, include_dependencies=False)
    if parent is None:
        raise ValueError(f"parent goal {parent_goal_id!r} not found")
    if not spec:
        return []

    children: list[Any] = []
    for entry in spec:
        title = str(entry.get("title") or "").strip()
        if not title:
            raise ValueError("each decomposition spec entry requires a title")
        kwargs: dict[str, Any] = {}
        # inherit from the parent, then let the spec override.
        for field in _INHERITED:
            kwargs[field] = getattr(parent, field)
        for key in _CHILD_KEYS:
            if key in entry:
                kwargs[key] = entry[key]
        kwargs["title"] = title
        kwargs["parent_goal_id"] = parent.id
        children.append(goal_store.create_goal(now=now, **kwargs))
    return children


__all__ = ["decompose_goal"]
