"""Bidding economy — a market-based ``goal_assignment`` policy (BDP-2597, Wave 4).

A :class:`BiddingAssignmentPolicy` is a ``goal_assignment`` registry impl (NOT the
default; selectable via ``OMNIGENT_USE_GOAL_ASSIGNMENT=bidding`` / per-tenant
config, ADR-0008). For an actionable unowned goal it runs a sealed auction over the
**same capability∩department candidate set** as ``assignment.resolve_assignee``:
each eligible agent produces a *bid*, and the highest valid bid wins.

``bid = f(confidence/capability_fit, realized-ROI, remaining agent budget)`` —
``compute_bid`` blends the goal's confidence (the optimizer's ROI weight), the
agent's realized-ROI track record from the scoreboard (Wave-3 learning signal), and
caps the result at the bidder's remaining budget so an agent can never promise to
spend more than it has. A zero remaining budget yields a zero (invalid) bid, so a
broke agent drops out and the goal goes to the next-best funded bidder — or waits if
nobody can fund it.

Same ``resolve_assignee(**kwargs)`` shape as :class:`DefaultAssignmentPolicy`, plus
one optional ``remaining_budget_fn`` seam (absent → unbounded, a pure ROI auction),
so the tick consumes it unchanged. Deterministic + testable with a fake
scoreboard/roster — no network, no LLM.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from bytedesk_omnigent.assignment import (
    AssignmentResolution,
    CandidateAgent,
    _candidate,
    _default_scoreboard,
)


def compute_bid(
    *, confidence: float, fit: float, realized_roi: float, remaining_budget: int | None
) -> float:
    """A single agent's bid for a goal (pure, deterministic).

    Blends the goal's ``confidence`` and the agent's ``fit`` (1.0 = eligible) with
    its ``realized_roi`` track record — a proven deliverer bids higher for the same
    goal — then caps the bid at ``remaining_budget`` so it can't outbid its own
    wallet. ``remaining_budget == 0`` → ``0.0`` (an invalid bid: the agent drops
    out). ``remaining_budget is None`` → unbounded (a pure ROI/confidence auction).
    """
    raw = confidence * fit * (1.0 + max(0.0, realized_roi))
    if remaining_budget is None:
        return raw
    if remaining_budget <= 0:
        return 0.0
    return min(raw, float(remaining_budget))


class BiddingAssignmentPolicy:
    """Market ``goal_assignment`` policy: the highest valid bid wins the goal."""

    def resolve_assignee(
        self,
        *,
        metric: str,
        roster: Sequence[CandidateAgent | object],
        explicit_owner: str | None = None,
        capability: str | None = None,
        department: str | None = None,
        scoreboard_fn: Callable[[str], list[tuple[str, float]]] = _default_scoreboard,
        remaining_budget_fn: Callable[[str], int | None] | None = None,
    ) -> AssignmentResolution:
        """Resolve who should own a goal by sealed-bid auction.

        Chain: **explicit owner** → **(capability∩department) eligibility** →
        **highest valid bid**. An explicit owner is never overridden (same invariant
        as the default policy). No eligible candidate, or no valid (>0) bid, →
        ``unassigned`` (the goal waits, no crash). Ties break in stable roster order.
        """
        if explicit_owner:
            return AssignmentResolution(
                assignee=explicit_owner, reason="explicit", ranked=(explicit_owner,)
            )

        candidates = [_candidate(e) for e in roster]
        eligible = [
            c
            for c in candidates
            if (capability is None or c.has_capability(capability))
            and (department is None or c.in_department(department))
        ]
        if not eligible:
            return AssignmentResolution(assignee=None, reason="unassigned", ranked=())

        scores = dict(scoreboard_fn(metric))
        budget_for = remaining_budget_fn or (lambda _agent_id: None)

        bids: list[tuple[float, CandidateAgent]] = []
        for c in eligible:
            bid = compute_bid(
                confidence=0.5,  # goal-level confidence; the tick's optimizer owns ranking
                fit=1.0,
                realized_roi=scores.get(c.agent_id, 0.0),
                remaining_budget=budget_for(c.agent_id),
            )
            bids.append((bid, c))

        # Stable: enumerate index keeps roster order for equal bids.
        ranked = sorted(
            enumerate(bids), key=lambda item: (-item[1][0], item[0])
        )
        ranked_ids = tuple(c.agent_id for _i, (_bid, c) in ranked)

        top_bid, top = ranked[0][1]
        if top_bid <= 0.0:
            # Nobody could fund a bid (all out of budget) → the goal waits.
            return AssignmentResolution(assignee=None, reason="unassigned", ranked=ranked_ids)
        return AssignmentResolution(assignee=top.agent_id, reason="bid", ranked=ranked_ids)


__all__ = ["BiddingAssignmentPolicy", "compute_bid"]
