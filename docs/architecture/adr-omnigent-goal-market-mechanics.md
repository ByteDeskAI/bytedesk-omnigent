# ADR: Goal market mechanics — bidding economy + contention arbitration (BDP-2592)

Status: Proposed
Date: 2026-06-27

## Context

Phase 1 assigns a goal to a static owner (`goal.owner_agent_id`); Phase 2 Wave 1
adds capability-based assignment (`assignment.py` via the `goal_assignment`
registry). Neither produces **price signals** or resolves **competition**: at
scale, many agents could work a goal and many goals compete for the same
actor/budget. The vision calls for a self-organizing internal economy where
effort flows to where it earns most, and contention is arbitrated, not raced.

The seams exist: `goal_assignment` is a `PluggableRegistry` seam (swap the
policy without forking); the treasury already does hierarchical caps +
`reserve`/`settle`; the optimizer already ranks by ROI; the scoreboard already
ranks agents.

## Decision

### Bidding economy — a `BiddingAssignmentPolicy`
A new `goal_assignment` registry impl. For an actionable goal, capable agents
(capability∩dept match) **bid** a `(budget, confidence)` pair; the dispatcher
funds the **best bid** (highest `confidence × capability_fit`, bounded by the
agent's remaining budget). Realized ROI from the outcome ledger feeds future bid
weight, so agents that actually deliver compound their share (the Phase-3
learning loop supplies the signal). Default remains the capability policy;
bidding is opt-in per tenant via `OMNIGENT_USE_GOAL_ASSIGNMENT` / config.

### Contention arbitration
When multiple ready goals contend for the same **actor, resource, or budget
scope**, an arbiter orders them by **tier × priority × ROI** before the tick
funds; losers wait (with a `waiting_reason`) rather than double-spawning or
racing. Arbitration runs inside the existing advisory-locked tick (single-writer,
ADR-0009) so it's multi-replica-safe.

### Invariants
1. Bidding/arbitration change **who/what order**, never the economic guards —
   budget caps, circuit breaker, blast-radius, and paper-trading still hold.
2. Both are `PluggableRegistry`/Strategy seams with the Phase-1 defaults intact;
   turning them off restores capability-assignment + ROI-order behaviour.
3. Exactly-once funding is preserved (guarded `reserve` + dispatch idempotency).

## Consequences
- Effort + budget self-organize toward realized ROI; specialization emerges from
  the scoreboard; contention is deterministic and explainable (arbitration is a
  decision-ledger entry).
- Most speculative part of the vision — shipped behind seams + off by default,
  arm-able per tenant after the core loop is proven.

## Phasing
Delivered in Wave 4 (BDP-2597), after the Wave-3 learning loop supplies bid/ROI
signal.
