"""Contention arbitration — order goals that compete for the same actor (BDP-2597).

When several ready goals are owned by the **same actor** they contend for that one
worker: the tick can only spawn one working session per agent at a time, so funding
all of them double-books the actor. :func:`arbitrate` groups goals by their
contention key (the owner agent), orders each group by **tier × priority × ROI**,
and returns the winners (one per contended actor, plus every uncontended goal) and
the losers (each paired with a ``waiting_reason``). The tick funds winners in the
returned order and stamps losers as waiting — they retry on a later tick once the
winner clears.

Pure + deterministic (ADR-0008 Strategy as a function behind a flag). Off → the
tick never calls this and behaviour is today's straight ROI order.

ponytail: contention key = owner agent only. Resource/budget-scope contention is a
richer grouping (a shared external resource, a shared sub-budget) — the seam is the
``_contention_key`` function; widen it when a non-actor contention case appears.
"""
from __future__ import annotations

from typing import Any

# Tier weight: org-wide goals outrank department/agent-scoped ones for the same
# actor (a company-level objective wins the worker's slot over a local task).
_TIER_RANK = {"org": 3, "department": 2, "team": 2, "agent": 1}


def _contention_key(goal: Any) -> str | None:
    """The scarce resource ``goal`` competes for, or ``None`` if it contends for none.

    Today: the owning actor. An unowned goal has no actor to contend for, so it is
    never a contender (returns ``None``).
    """
    return getattr(goal, "owner_agent_id", None)


def _arb_score(goal: Any) -> tuple:
    """Sort key: tier desc, priority asc (lower number = more urgent), ROI desc.

    ROI uses the goal's own ``expected_value_cents * confidence`` (a unit budget
    denominator) — the same numerator the optimizer ranks by — so arbitration order
    is consistent with ROI ranking within a contention group. ``created_at`` + ``id``
    break remaining ties deterministically.
    """
    tier = _TIER_RANK.get(getattr(goal, "tier", "org"), 2)
    roi = goal.expected_value_cents * goal.confidence
    return (-tier, goal.priority, -roi, getattr(goal, "created_at", 0), goal.id)


def arbitrate(goals: list[Any]) -> tuple[list[Any], list[tuple[Any, str]]]:
    """Resolve contention among ``goals`` competing for the same actor.

    Returns ``(winners, losers)`` where ``winners`` is the fundable set (each
    contended actor's top goal by :func:`_arb_score`, plus every uncontended goal)
    preserving the input order, and ``losers`` pairs each blocked goal with a
    ``waiting_reason``. A goal with no contention key (no owner) never contends.
    """
    # Group goals by the resource they contend for; None = uncontended.
    groups: dict[str, list[Any]] = {}
    uncontended: list[Any] = []
    for goal in goals:
        key = _contention_key(goal)
        if key is None:
            uncontended.append(goal)
        else:
            groups.setdefault(key, []).append(goal)

    winners_set: set[str] = set()
    losers: list[tuple[Any, str]] = []
    for key, contenders in groups.items():
        if len(contenders) == 1:
            winners_set.add(contenders[0].id)
            continue
        ordered = sorted(contenders, key=_arb_score)
        winners_set.add(ordered[0].id)
        for blocked in ordered[1:]:
            losers.append(
                (blocked, f"contention: actor {key} funded for goal {ordered[0].id}")
            )

    winner_ids = winners_set | {g.id for g in uncontended}
    winners = [g for g in goals if g.id in winner_ids]
    return winners, losers


__all__ = ["arbitrate"]
