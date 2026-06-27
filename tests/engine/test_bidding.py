"""Bidding economy (BDP-2597, Phase 2 Wave 4).

A ``BiddingAssignmentPolicy`` is a ``goal_assignment`` registry impl (NOT the
default). Capable agents (the same capability∩department candidate set as
``assignment.py``) bid; the highest valid bid wins, bounded by the bidder's
remaining budget and weighted by realized ROI from the scoreboard.

Fakes only, no network/LLM.
"""
from __future__ import annotations

from bytedesk_omnigent.assignment import CandidateAgent
from bytedesk_omnigent.engine.bidding import BiddingAssignmentPolicy, compute_bid


def _scoreboard(mapping):
    """A scoreboard_fn that ignores the metric and returns a fixed ranking."""
    return lambda metric: list(mapping.items())


# -- compute_bid (pure) ------------------------------------------------------
def test_compute_bid_zero_without_budget() -> None:
    # No remaining budget → an agent cannot bid (a zero/negative bid is invalid).
    assert compute_bid(confidence=0.9, fit=1.0, realized_roi=2.0, remaining_budget=0) == 0.0


def test_compute_bid_grows_with_realized_roi() -> None:
    base = compute_bid(confidence=0.5, fit=1.0, realized_roi=0.0, remaining_budget=1000)
    proven = compute_bid(confidence=0.5, fit=1.0, realized_roi=5.0, remaining_budget=1000)
    assert proven > base  # a proven deliverer bids higher for the same goal


def test_compute_bid_bounded_by_budget() -> None:
    # The bid is capped at the bidder's remaining budget — it can't promise to spend
    # more than it has.
    bid = compute_bid(confidence=1.0, fit=1.0, realized_roi=100.0, remaining_budget=10)
    assert bid <= 10


# -- BiddingAssignmentPolicy -------------------------------------------------
def test_bidding_picks_best_bid_among_candidates() -> None:
    policy = BiddingAssignmentPolicy()
    roster = [
        CandidateAgent(agent_id="alice", capabilities=("dotnet",)),
        CandidateAgent(agent_id="bob", capabilities=("dotnet",)),
    ]
    # bob has the higher realized ROI on the scoreboard → bob outbids alice.
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=roster,
        capability="dotnet",
        scoreboard_fn=_scoreboard({"alice": 1.0, "bob": 9.0}),
        remaining_budget_fn=lambda agent_id: 1000,
    )
    assert resolution.assignee == "bob"
    assert resolution.reason == "bid"


def test_past_winner_outbids() -> None:
    # Same goal, two equally-capable agents. The one with the realized-ROI track
    # record (the past winner) wins the auction.
    policy = BiddingAssignmentPolicy()
    roster = [
        CandidateAgent(agent_id="rookie", capabilities=("seo",)),
        CandidateAgent(agent_id="veteran", capabilities=("seo",)),
    ]
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=roster,
        capability="seo",
        scoreboard_fn=_scoreboard({"veteran": 8.0}),  # rookie has no track record
        remaining_budget_fn=lambda agent_id: 1000,
    )
    assert resolution.assignee == "veteran"


def test_bid_bounded_by_remaining_budget_changes_winner() -> None:
    # alice has the better ROI but is out of budget → bob (with budget) wins.
    policy = BiddingAssignmentPolicy()
    roster = [
        CandidateAgent(agent_id="alice", capabilities=("ops",)),
        CandidateAgent(agent_id="bob", capabilities=("ops",)),
    ]
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=roster,
        capability="ops",
        scoreboard_fn=_scoreboard({"alice": 9.0, "bob": 1.0}),
        remaining_budget_fn=lambda agent_id: 0 if agent_id == "alice" else 1000,
    )
    assert resolution.assignee == "bob"


def test_no_valid_bids_falls_back_to_unassigned() -> None:
    # Every candidate is out of budget → no valid bid → unassigned (the goal waits).
    policy = BiddingAssignmentPolicy()
    roster = [CandidateAgent(agent_id="alice", capabilities=("ops",))]
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=roster,
        capability="ops",
        scoreboard_fn=_scoreboard({"alice": 9.0}),
        remaining_budget_fn=lambda agent_id: 0,
    )
    assert resolution.assignee is None
    assert resolution.reason == "unassigned"


def test_explicit_owner_short_circuits_bidding() -> None:
    # A deliberate owner is never overridden by the auction (same invariant as the
    # default policy).
    policy = BiddingAssignmentPolicy()
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=[CandidateAgent(agent_id="alice", capabilities=("ops",))],
        capability="ops",
        explicit_owner="ceo",
        scoreboard_fn=_scoreboard({"alice": 9.0}),
        remaining_budget_fn=lambda agent_id: 1000,
    )
    assert resolution.assignee == "ceo"
    assert resolution.reason == "explicit"


def test_no_eligible_candidate_is_unassigned() -> None:
    # Capability filter excludes everyone → unassigned (no crash).
    policy = BiddingAssignmentPolicy()
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=[CandidateAgent(agent_id="alice", capabilities=("dotnet",))],
        capability="python",
        scoreboard_fn=_scoreboard({"alice": 9.0}),
        remaining_budget_fn=lambda agent_id: 1000,
    )
    assert resolution.assignee is None


def test_default_remaining_budget_unbounded() -> None:
    # No remaining_budget_fn injected → bids are unbounded by budget (behaviour
    # falls back to a pure ROI/confidence auction). Highest scoreboard wins.
    policy = BiddingAssignmentPolicy()
    roster = [
        CandidateAgent(agent_id="alice", capabilities=("ops",)),
        CandidateAgent(agent_id="bob", capabilities=("ops",)),
    ]
    resolution = policy.resolve_assignee(
        metric="goal",
        roster=roster,
        capability="ops",
        scoreboard_fn=_scoreboard({"alice": 9.0, "bob": 1.0}),
    )
    assert resolution.assignee == "alice"


def test_bidding_registered_in_assignment_registry(monkeypatch) -> None:
    # The bidding policy is selectable via OMNIGENT_USE_GOAL_ASSIGNMENT=bidding,
    # while the registry default stays the capability policy.
    from bytedesk_omnigent.engine.registries import build_assignment_registry

    reg = build_assignment_registry()
    assert type(reg.resolve_default()).__name__ == "DefaultAssignmentPolicy"
    monkeypatch.setenv("OMNIGENT_USE_GOAL_ASSIGNMENT", "bidding")
    assert isinstance(reg.resolve_default(), BiddingAssignmentPolicy)
