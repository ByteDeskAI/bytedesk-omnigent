"""Per-model cost estimate for one goal's agent work (BDP-2594, Phase 2 Wave 1).

Phase 1 charged a flat ``_DEFAULT_EST_COST`` per goal regardless of model. This
replaces that with a tiny per-model price table × an estimated tokens-per-goal, so
the treasury reserves a cost that tracks the model a goal will run on.

It is deliberately a small lookup, not a billing system: a ``{model: cents/1k}``
map with a default for unknown models, multiplied by a tokens estimate (caller
override → config default). Realized/actual cost still settles through
``treasury.settle`` once the goal completes; this is only the *estimate*.
"""
from __future__ import annotations

# Cents per 1k tokens, rounded to whole cents (blended in+out). A coarse tier map
# — the point is relative ordering (a premium model reserves more than a mini),
# not invoice accuracy. Unknown models fall back to _DEFAULT_PRICE.
# ponytail: hand-maintained table; pull from a live price feed only if it drifts.
MODEL_PRICE_CENTS_PER_1K: dict[str, int] = {
    "gpt-5.5": 3,
    "gpt-5": 2,
    "gpt-5-mini": 1,
    "claude-opus": 4,
    "claude-sonnet": 2,
    "claude-haiku": 1,
}
_DEFAULT_PRICE = 2

# A coarse "one goal's agent turn" token budget when the caller has no better
# number. ~30k tokens covers a reasoning turn + a few tool calls.
DEFAULT_TOKENS_PER_GOAL = 30_000

# The default model assumed when a goal does not name one.
DEFAULT_MODEL = "gpt-5"


def estimate_goal_cost_cents(
    *,
    model: str | None = None,
    tokens: int | None = None,
) -> int:
    """Estimate the cents one goal's agent work will cost on ``model``.

    ``price_per_1k(model) * tokens / 1000``, floored at 1 cent so a goal always
    reserves something. Unknown model → the default price; ``tokens=None`` →
    :data:`DEFAULT_TOKENS_PER_GOAL`.
    """
    price = MODEL_PRICE_CENTS_PER_1K.get(model or DEFAULT_MODEL, _DEFAULT_PRICE)
    token_count = DEFAULT_TOKENS_PER_GOAL if tokens is None else tokens
    return max(1, round(price * token_count / 1000))


def goal_est_cost_cents(goal, *, default_tokens: int | None = None) -> int:
    """Estimate the reservation cost for ``goal`` from its model + a token budget.

    Reads the model from ``goal.payload['model']`` (the model the dispatched
    session will run on) when present, else the default. Token budget comes from
    ``default_tokens`` (config) when given, else :data:`DEFAULT_TOKENS_PER_GOAL`.
    """
    payload = getattr(goal, "payload", None) or {}
    model = payload.get("model") if isinstance(payload, dict) else None
    return estimate_goal_cost_cents(model=model, tokens=default_tokens)


def actual_cost_cents(goal, *, fallback: int) -> int:
    """Derive a completed goal's actual cost, else the ``fallback`` estimate.

    ponytail: in-process session usage is not reachable from the sync tick (the
    runner records usage out-of-band), so a goal that carries a measured cost in
    ``payload['actual_cost_cents']`` settles to it; otherwise we settle to the
    reserved estimate (delta 0 — a no-op correction). This is the known
    simplification: wire real session usage here when it becomes in-process.
    """
    payload = getattr(goal, "payload", None) or {}
    if isinstance(payload, dict):
        measured = payload.get("actual_cost_cents")
        if isinstance(measured, int) and measured >= 0:
            return measured
    return fallback


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TOKENS_PER_GOAL",
    "MODEL_PRICE_CENTS_PER_1K",
    "actual_cost_cents",
    "estimate_goal_cost_cents",
    "goal_est_cost_cents",
]
