"""Goal resolver — condition tree → ``actionable`` + ``waiting_reasons`` (BDP-2584).

The general readiness layer over Phase 1. A goal that carries a condition AST in
``payload["condition"]`` is gated by that tree, evaluated against the sensor
registry. A goal with **no** AST is resolved from its existing ``dependencies`` +
``readiness_kind``, deriving a tree that reproduces ``goals._activation_for``
exactly — so every legacy goal resolves identically and nothing is migrated.

**No new column.** The AST persists inside the existing ``goal.payload`` JSON under
``CONDITION_PAYLOAD_KEY`` — no migration, the payload column already round-trips
arbitrary JSON. (The Phase 1 cadence work added a column; this layer deliberately
does not, since the payload already carries delivery/hierarchy state next to it.)

The resolver groups the tree's leaves by ``(sensor, query)``, asks each sensor
once, then walks the tree. ``waiting_reasons`` names every **unmet leaf** in a
human-readable form; ``freshness_s`` is the min ``stale_after_s`` across readings
(``None`` when nothing was read), so a caller can decide when to re-resolve.
"""
from __future__ import annotations

from typing import Any

from bytedesk_omnigent.engine.conditions import (
    All,
    ConditionNode,
    Leaf,
    Predicate,
    Readings,
    from_dict,
)
from bytedesk_omnigent.engine.sensors import SensorContext

# Where the condition AST lives inside ``goal.payload`` (no DB column).
CONDITION_PAYLOAD_KEY = "condition"

ResolveResult = dict[str, Any]


def _payload_condition(goal: Any) -> ConditionNode | None:
    payload = getattr(goal, "payload", None) or {}
    raw = payload.get(CONDITION_PAYLOAD_KEY)
    return from_dict(raw) if isinstance(raw, dict) else None


def _legacy_condition(goal: Any) -> ConditionNode:
    """Derive a tree from ``dependencies`` + ``readiness_kind`` (≡ ``_activation_for``).

    - ``deferred`` → an always-false leaf (paused; never actionable).
    - ``dependent`` → ``All`` of one ``manual exists`` leaf per dependency (ready
      when every dep status != pending — exactly the legacy rule).
    - otherwise (immediate / no deps) → ``All([])`` (vacuously true → actionable).
    """
    if goal.readiness_kind == "deferred":
        # A leaf that can never be satisfied: a time window that has already closed.
        return Leaf("time", {"within": [1, 0]}, Predicate("exists"))
    if goal.readiness_kind == "dependent":
        return All(
            [Leaf("manual", {"dep_id": dep.id}, Predicate("exists")) for dep in goal.dependencies]
        )
    return All([])


def _reason(leaf: Leaf, reading: dict | None) -> str:
    query = " ".join(f"{k}={leaf.query[k]}" for k in sorted(leaf.query))
    pred = leaf.predicate.op
    if leaf.predicate.operand is not None:
        pred = f"{pred} {leaf.predicate.operand!r}"
    saw = "" if reading is None else f" (saw {reading.get('value')!r})"
    return f"waiting: {leaf.sensor} {query} {pred}{saw}".strip()


def resolve(
    goal: Any,
    *,
    registry: Any,
    goal_store: Any,
    now: int,
) -> ResolveResult:
    """Resolve whether ``goal`` is actionable now, and why not if it isn't.

    :param registry: a :class:`~bytedesk_omnigent.engine.sensors.SensorRegistry`.
    :returns: ``{"actionable": bool, "waiting_reasons": [str], "freshness_s": int|None}``.
    """
    tree = _payload_condition(goal) or _legacy_condition(goal)
    leaves = tree.leaves()

    ctx = SensorContext(goal=goal, goal_store=goal_store, now=now)
    readings: Readings = {}
    freshness: list[int] = []
    for leaf in leaves:
        key = leaf.reading_key()
        if key in readings:
            continue
        reading = registry.get(leaf.sensor).evaluate(leaf.query, ctx)
        readings[key] = reading
        stale = reading.get("stale_after_s")
        if stale is not None:
            freshness.append(stale)

    actionable = tree.eval(readings)
    waiting_reasons: list[str] = []
    if not actionable:
        for leaf in leaves:
            if not leaf.eval(readings):
                waiting_reasons.append(_reason(leaf, readings.get(leaf.reading_key())))

    return {
        "actionable": actionable,
        "waiting_reasons": waiting_reasons,
        "freshness_s": min(freshness) if freshness else None,
    }


def evaluate_success_condition(
    goal: Any,
    *,
    registry: Any,
    goal_store: Any,
    now: int,
) -> bool:
    """True iff ``goal.success_condition`` (a condition-AST dict) is satisfied now.

    "Done is evaluated, not declared" (BDP-2594): a goal that carries a
    ``success_condition`` is complete when its tree resolves true against the
    sensor registry — the SAME machinery :func:`resolve` uses, just reading the
    success tree instead of the readiness tree. A goal with no
    ``success_condition`` returns ``False`` (never auto-completed).
    """
    raw = getattr(goal, "success_condition", None)
    tree = from_dict(raw) if isinstance(raw, dict) else None
    if tree is None:
        return False

    ctx = SensorContext(goal=goal, goal_store=goal_store, now=now)
    readings: Readings = {}
    for leaf in tree.leaves():
        key = leaf.reading_key()
        if key in readings:
            continue
        readings[key] = registry.get(leaf.sensor).evaluate(leaf.query, ctx)
    return tree.eval(readings)


__all__ = [
    "CONDITION_PAYLOAD_KEY",
    "ResolveResult",
    "evaluate_success_condition",
    "resolve",
]
