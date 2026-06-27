"""ROI optimizer — rank the actionable frontier by risk-decayed ROI (BDP-2585).

ADR-0008: ``Optimizer`` is a Protocol with a default so a tenant can swap the
ranking policy. The default ranks **descending** by

    score = roi(goal, remaining_budget) * RISK_DECAY[risk_tier]

where ``roi = (expected_value_cents * confidence) / max(remaining_budget, 1)``
(``bytedesk_omnigent.goals.roi``) and the risk multiplier discounts riskier
goals (low 1.0, medium 0.7, high 0.4). Ties break deterministically on
``(priority, created_at, id)`` so the order is stable and replayable.

Pure + side-effect-free — the tick does the funding; the optimizer only orders.
``remaining_budget`` is not known per-goal here (that lives in the Treasury), so
the score uses each goal's own ``expected_value * confidence`` as the ROI
numerator with a unit denominator — i.e. ranks by **risk-decayed expected
value**, which preserves ROI order within a tier (the per-tier budget partition +
funding cutoff happen in the tick against the live Treasury).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Risk discounts the score: a high-risk goal must clear a higher EV bar to outrank
# a safe one (the blast-radius cost of being wrong).
RISK_DECAY = {"low": 1.0, "medium": 0.7, "high": 0.4}


def _score(goal: Any) -> float:
    decay = RISK_DECAY.get(goal.risk_tier, 1.0)
    return goal.expected_value_cents * goal.confidence * decay


@runtime_checkable
class Optimizer(Protocol):
    """Ranking policy (ADR-0008)."""

    def rank(self, goals: list[Any], *, now: int) -> list[Any]: ...


class RoiOptimizer:
    """Default: rank by risk-decayed ROI desc, stable tie-break (BDP-2585)."""

    def rank(self, goals: list[Any], *, now: int) -> list[Any]:
        del now  # time-decay hook (deadline urgency) — Phase 6; order is EV-driven now.
        return sorted(
            goals,
            key=lambda g: (-_score(g), g.priority, g.created_at, g.id),
        )


__all__ = ["RISK_DECAY", "Optimizer", "RoiOptimizer"]
