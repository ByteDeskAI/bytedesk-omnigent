"""Frontier read — the actionable+ranked goal set for the command center (BDP-2598).

A read-only projection that mirrors what the tick *would* fund: take the ready/
owned candidate frontier, keep the ones the resolver says are ``actionable`` (and
carry their ``waiting_reasons`` when they're not), score each by the same
risk-decayed ROI the :class:`RoiOptimizer` ranks on, and return them ranked.

Pure + side-effect-free (ADR-0008): it ranks through the injected ``Optimizer``
and reads ROI via ``bytedesk_omnigent.goals.roi`` — the same primitives the tick
uses — so the cockpit view never diverges from the engine. The Treasury (when
given) supplies each goal's remaining-budget denominator for ROI; without it the
ROI numerator stands alone (rank order is unchanged).
"""
from __future__ import annotations

from typing import Any

from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.goals import roi


def _candidates(goal_store: Any) -> list[Any]:
    """The ready, immediate, owned goals — the rows the tick considers each tick."""
    return [
        g
        for g in goal_store.list_goals(
            status="assigned", activation_state="ready", include_dependencies=True
        )
        if g.cadence_kind == "immediate" and g.owner_agent_id
    ]


def _remaining_budget(treasury: Any, goal: Any) -> int:
    if treasury is None:
        return 1
    remaining = treasury.remaining_cents(goal.tier, goal.target_id)
    return remaining if remaining is not None else 1


def build_frontier(
    *,
    goal_store: Any,
    optimizer: Any | None = None,
    sensor_registry: Any = None,
    treasury: Any = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    now: int = 0,
) -> list[dict[str, Any]]:
    """The actionable+ranked frontier with ROI + waiting_reasons (read).

    :param optimizer: ranking policy (default :class:`RoiOptimizer`).
    :param sensor_registry: when given, the resolver gates each goal on its
        condition tree and supplies ``waiting_reasons`` for the not-yet-actionable;
        without it every candidate is treated actionable (Phase-2 behaviour).
    :returns: a list of ``{goal_id, title, status, priority, target_kind,
        target_id, risk_tier, expected_value_cents, confidence, roi, actionable,
        waiting_reasons}`` dicts, ranked by risk-decayed ROI (best first).
    """
    optimizer = optimizer or RoiOptimizer()
    candidates = [
        g
        for g in _candidates(goal_store)
        if (target_kind is None or g.target_kind == target_kind)
        and (target_id is None or g.target_id == target_id)
    ]

    reasons: dict[str, list[str]] = {}
    if sensor_registry is not None:
        from bytedesk_omnigent.engine.resolver import resolve

        actionable: list[Any] = []
        for g in candidates:
            result = resolve(g, registry=sensor_registry, goal_store=goal_store, now=now)
            if result["actionable"]:
                actionable.append(g)
            else:
                reasons[g.id] = result["waiting_reasons"]
        ranked = optimizer.rank(actionable, now=now)
    else:
        ranked = optimizer.rank(candidates, now=now)

    return [
        {
            "goal_id": g.id,
            "title": g.title,
            "status": g.status,
            "priority": g.priority,
            "target_kind": g.target_kind,
            "target_id": g.target_id,
            "risk_tier": g.risk_tier,
            "expected_value_cents": g.expected_value_cents,
            "confidence": g.confidence,
            "roi": roi(g, remaining_budget_cents=_remaining_budget(treasury, g)),
            "actionable": True,
            "waiting_reasons": [],
        }
        for g in ranked
    ] + [
        {
            "goal_id": g.id,
            "title": g.title,
            "status": g.status,
            "priority": g.priority,
            "target_kind": g.target_kind,
            "target_id": g.target_id,
            "risk_tier": g.risk_tier,
            "expected_value_cents": g.expected_value_cents,
            "confidence": g.confidence,
            "roi": roi(g, remaining_budget_cents=_remaining_budget(treasury, g)),
            "actionable": False,
            "waiting_reasons": reasons[g.id],
        }
        for g in candidates
        if g.id in reasons
    ]


__all__ = ["build_frontier"]
