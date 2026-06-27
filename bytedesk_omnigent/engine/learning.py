"""Learning loop — confidence + cost estimates learn from realized outcomes
(BDP-2596 Wave 3, feature 1).

The engine stores ``confidence`` (the optimizer's ROI weight) statically at 0.5.
This module moves it toward the *realized-vs-expected* ratio once a goal completes
with a booked outcome (an EWMA, clamped to [0, 1]). A goal that consistently
beats its expected value drifts toward 1.0 (winners bias future ranking, since the
optimizer already multiplies by confidence); a goal that under-delivers drifts
down. Goals with no expected value (legacy / static) emit no signal and are left
untouched — no learning regression.

The cost model's tokens-per-goal estimate also learns: ``learn_tokens_per_goal``
reads the *measured* actual costs of completed goals and converts the mean back to
a tokens estimate, so the treasury reserves a number that tracks reality instead of
the coarse hand-set default.

Pure functions + one store read; no network/LLM. ADR-0008: the EWMA alpha is the
only knob, exposed as a default so a strategy could swap it later.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.cost import (
    _DEFAULT_PRICE,
    DEFAULT_MODEL,
    DEFAULT_TOKENS_PER_GOAL,
    MODEL_PRICE_CENTS_PER_1K,
)

# EWMA weight on the newest observation. 0.3 = responsive but not whiplash —
# ~3 consistent outcomes move confidence most of the way.
# ponytail: single global alpha; make it a per-tenant config knob only if tuning demands.
_ALPHA = 0.3


def update_confidence(*, confidence: float, realized: int, expected: int) -> float:
    """EWMA ``confidence`` toward the realized/expected ROI ratio, clamped [0, 1].

    ``expected <= 0`` → no signal (returns ``confidence`` unchanged), so legacy /
    static goals never learn. The ratio is clamped to [0, 1] before blending: a
    goal that hit its expectation pulls confidence toward 1.0, a miss toward its
    fractional ratio.
    """
    if expected <= 0:
        return confidence
    ratio = realized / expected
    target = min(1.0, max(0.0, ratio))
    blended = (1 - _ALPHA) * confidence + _ALPHA * target
    return min(1.0, max(0.0, blended))


def apply_completion_learning(goal_store, goal, *, now: int | None = None) -> None:
    """Learn the goal's confidence from its realized-vs-expected value on completion.

    Reads the goal's now-final ``realized_value_cents`` (booked only by
    ``treasury.book_outcome``) against its ``expected_value_cents`` and persists the
    EWMA-updated confidence via ``update_goal``. A no-signal goal (expected 0) is a
    no-op write-skip. Idempotent enough for the tick: re-running on an already-done
    goal nudges confidence again toward the same target, which converges.
    """
    new_confidence = update_confidence(
        confidence=goal.confidence,
        realized=goal.realized_value_cents,
        expected=goal.expected_value_cents,
    )
    if new_confidence == goal.confidence:
        return
    goal_store.update_goal(goal_id=goal.id, confidence=new_confidence, now=now)


def learn_tokens_per_goal(goal_store, *, default: int = DEFAULT_TOKENS_PER_GOAL) -> int:
    """Estimate tokens-per-goal from the measured cost of completed goals.

    Reads ``payload['actual_cost_cents']`` off ``done`` goals (the only place an
    in-process measured cost lands, see ``engine.cost.actual_cost_cents``), averages
    it, and converts cents → tokens at the default model price. No measured history
    → the supplied ``default`` (behaviour-preserving for a cold engine).

    ponytail: prices at the DEFAULT model (the engine doesn't bill per-model at the
    treasury reserve point). Per-model token learning is a richer split — defer until
    the per-model spread actually matters.
    """
    measured: list[int] = []
    for goal in goal_store.list_goals(status="done"):
        payload = goal.payload or {}
        cost = payload.get("actual_cost_cents") if isinstance(payload, dict) else None
        if isinstance(cost, int) and cost >= 0:
            measured.append(cost)
    if not measured:
        return default
    mean_cents = sum(measured) / len(measured)
    price = MODEL_PRICE_CENTS_PER_1K.get(DEFAULT_MODEL, _DEFAULT_PRICE)
    return max(1, round(mean_cents / price * 1000))


__all__ = [
    "apply_completion_learning",
    "learn_tokens_per_goal",
    "update_confidence",
]
