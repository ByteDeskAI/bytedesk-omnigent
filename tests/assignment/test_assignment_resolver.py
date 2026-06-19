"""Tests for the assignment resolver: explicit owner → (capability ∩ department)
→ scoreboard rank (BDP-2335, ADR-0142).

The ranking step reuses the find_specialist scoreboard verbatim, so the resolver
learns from recorded outcomes — but only AMONG the agents the capability ∩
department filter leaves eligible. Filter first, rank second.
"""

from __future__ import annotations

from bytedesk_omnigent.assignment import (
    AssignmentResolution,
    CandidateAgent,
    resolve_assignee,
)
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.outcomes import SqlAlchemyOutcomeLedger


def _roster() -> list[CandidateAgent]:
    return [
        CandidateAgent(agent_id="priya", department="eng", capabilities=("dotnet", "review")),
        CandidateAgent(agent_id="elias", department="eng", capabilities=("dotnet",)),
        CandidateAgent(agent_id="mara", department="sales", capabilities=("dotnet",)),
        CandidateAgent(agent_id="caleb", department="eng", capabilities=("design",)),
    ]


# ── chain link 1: explicit owner wins ────────────────────────────────


def test_explicit_owner_short_circuits_filter_and_rank() -> None:
    # ag_owner is not even in the roster and holds no capability — it still wins.
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        explicit_owner="ag_owner",
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [("priya", 999.0)],
    )
    assert res == AssignmentResolution(
        assignee="ag_owner", reason="explicit", ranked=("ag_owner",)
    )


# ── chain link 2a: capability ∩ department filter ────────────────────


def test_capability_filter_excludes_agents_without_the_slug() -> None:
    # caleb is in eng but lacks 'dotnet'; mara has 'dotnet' but is in sales.
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [],
    )
    # only priya + elias are eligible (dotnet ∩ eng).
    assert set(res.ranked) == {"priya", "elias"}
    assert "caleb" not in res.ranked
    assert "mara" not in res.ranked


def test_high_scorer_without_capability_is_never_assigned() -> None:
    # caleb tops the scoreboard but lacks 'dotnet' → filter beats rank.
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [("caleb", 1000.0), ("elias", 5.0), ("priya", 1.0)],
    )
    assert res.assignee == "elias"
    assert res.reason == "ranked"


def test_no_eligible_candidate_is_unassigned() -> None:
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="kubernetes",  # nobody holds it
        scoreboard_fn=lambda _m: [("priya", 10.0)],
    )
    assert res.assignee is None
    assert res.reason == "unassigned"
    assert res.ranked == ()


def test_absent_filters_are_no_ops() -> None:
    # No capability + no department → whole roster is eligible.
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        scoreboard_fn=lambda _m: [("mara", 50.0)],
    )
    assert res.assignee == "mara"
    assert set(res.ranked) == {"priya", "elias", "mara", "caleb"}


# ── chain link 2b: scoreboard rank of the survivors ──────────────────


def test_eligible_survivors_rank_by_scoreboard() -> None:
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [("elias", 9.0), ("priya", 2.0)],
    )
    assert res.ranked == ("elias", "priya")
    assert res.assignee == "elias"


def test_unscored_eligible_agent_sorts_last_but_is_still_assignable() -> None:
    # priya scored, elias has no recorded outcome → priya first, elias still ranked.
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [("priya", 3.0)],
    )
    assert res.ranked == ("priya", "elias")
    assert res.assignee == "priya"
    assert res.reason == "ranked"


def test_all_unscored_falls_back_to_stable_roster_order() -> None:
    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [],
    )
    # No scores anywhere → roster order preserved, reason is 'fallback'.
    assert res.ranked == ("priya", "elias")
    assert res.assignee == "priya"
    assert res.reason == "fallback"


# ── normalization: immutable capability sequence contract ────────────


def test_candidate_normalizes_capabilities_to_immutable_tuple() -> None:
    caps = ["dotnet", "review"]
    cand = CandidateAgent(agent_id="x", capabilities=caps)  # type: ignore[arg-type]
    assert cand.capabilities == ("dotnet", "review")
    caps.append("mutated")  # mutating the source must not leak into the candidate.
    assert cand.capabilities == ("dotnet", "review")


def test_resolver_coerces_duck_typed_records() -> None:
    class _Record:
        agent_id = "duck"
        department = "eng"
        capabilities = ("dotnet",)

    res = resolve_assignee(
        metric="ships",
        roster=[_Record()],
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda _m: [("duck", 1.0)],
    )
    assert res.assignee == "duck"


# ── integration: rank by REAL recorded outcomes (scoreboard verbatim) ─


def test_ranks_by_real_scoreboard_from_outcome_ledger(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    ledger = SqlAlchemyOutcomeLedger(db)
    goals = SqlAlchemyGoalStore(db)
    # elias delivered more 'ships' than priya → ranks first among the eligible.
    ledger.record_outcome(agent_id="priya", kind="feature_shipped", metric="ships", value=1, now=1)
    ledger.record_outcome(agent_id="elias", kind="feature_shipped", metric="ships", value=3, now=2)
    # mara has the most ships but is in sales → filtered out by department.
    ledger.record_outcome(agent_id="mara", kind="feature_shipped", metric="ships", value=99, now=3)

    res = resolve_assignee(
        metric="ships",
        roster=_roster(),
        capability="dotnet",
        department="eng",
        scoreboard_fn=lambda m: goals.scoreboard(metric=m, limit=1000),
    )
    assert res.assignee == "elias"
    assert res.ranked == ("elias", "priya")
    assert "mara" not in res.ranked
