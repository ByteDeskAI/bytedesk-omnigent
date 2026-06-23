"""Built-in hard-stop budget circuit breaker (BDP-2271, ADR-0142).

The scariest gap for an always-on org: the existing ``cost_budget`` policy is a
**downgrade gate**, not a stop — at the ceiling it DENYs only while on an
expensive model, tells the agent to ``/model`` down, then re-allows. A runaway
loop spends all night on a cheaper model.

``cost_hard_stop`` is a true circuit breaker: an **unconditional DENY** once
cumulative session spend reaches a hard USD ceiling, on both the ``request`` and
``tool_call`` phases — no model check, no downgrade-and-re-allow. The lineage is
parked for human review rather than spending past the ceiling. Pairs with
``cost_budget`` (keep the downgrade gate as the soft tier; this is the hard floor).

Reads cumulative spend from ``event["context"]["usage"]["total_cost_usd"]`` — the
same server-maintained session total ``cost_budget`` uses. When pricing is
unavailable the cost stays ``0.0`` and the breaker never trips (it cannot stop
what it cannot price).
"""

from __future__ import annotations

from bytedesk_omnigent.policies import PolicyRegistryRaw
from bytedesk_omnigent.policies._floors import (
    COST_CEILING_SANITY_USD,
    require_positive_finite,
)
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# Budgeted phases — before the model runs (so text-only turns count) and before
# each tool call. Mirrors cost_budget's enforcement points.
_BUDGETED_PHASES = frozenset({"request", "tool_call"})


def _spent_usd(event: PolicyEvent) -> float:
    context = event.get("context") or {}
    usage = context.get("usage") or {}
    raw = usage.get("total_cost_usd", 0.0)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cost_hard_stop(max_cost_usd: float) -> PolicyCallable:
    """Factory: unconditionally deny once session spend reaches *max_cost_usd*.

    Unlike ``cost_budget`` (a downgrade gate), this never re-allows — once the
    hard ceiling is hit the turn / tool call is denied regardless of model, so an
    autonomous agent cannot ASK-then-proceed or downgrade-then-continue past it.

    :param max_cost_usd: The hard USD ceiling for cumulative session spend.
    :returns: A policy callable that DENYs at/above the ceiling.
    :raises PolicyFloorError: if *max_cost_usd* is not a finite, positive,
        non-absurd number — a breaker with no usable ceiling can never trip.
    """
    max_cost_usd = require_positive_finite(
        "max_cost_usd", max_cost_usd, COST_CEILING_SANITY_USD
    )

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") not in _BUDGETED_PHASES:
            return _ALLOW
        spent = _spent_usd(event)
        if spent >= max_cost_usd:
            return {
                "result": "DENY",
                "reason": (
                    f"hard budget ceiling reached: ${spent:.4f} >= ${max_cost_usd} "
                    "— work parked, no downgrade (ADR-0142 circuit breaker)"
                ),
            }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.budget.cost_hard_stop",
        "kind": "factory",
        "name": "Hard-Stop Budget Circuit Breaker",
        "description": "Unconditionally denies once cumulative session spend reaches a "
        "hard USD ceiling — a true stop (not cost_budget's downgrade gate), ADR-0142.",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_cost_usd": {
                    "type": "number",
                    "description": "Hard USD ceiling for cumulative session spend",
                },
            },
            "required": ["max_cost_usd"],
        },
    },
]
