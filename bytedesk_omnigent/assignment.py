"""Assignment resolver — capability filter over the self-learning scoreboard
(BDP-2335, ADR-0142).

Routing a piece of work to the *right* agent is a two-stage decision, in order:

1. **Explicit owner wins.** If the work already names an owner, that is the
   assignment — no inference, no override. (A manager's deliberate choice, or a
   goal that already carries ``owner_agent_id``.)
2. **Eligibility filter, then merit rank.** Otherwise narrow the roster to the
   agents who are actually *allowed* + *placed* to do the work — they hold the
   required ``capability`` slug AND sit in the required ``department`` (the
   capability ∩ department intersection) — and only THEN rank the survivors by
   what has actually worked, reusing the ``scoreboard_entries`` the Business
   Outcome Ledger upserts (``bytedesk_omnigent/outcomes.py`` →
   ``bytedesk_omnigent/goals.py`` scoreboard). Filter on capability first, rank
   second: a high scorer who lacks the capability is never assigned.

This reuses the ``find_specialist`` ranking step **verbatim** — the same
``get_goal_store().scoreboard(metric=...)`` call — so the resolver inherits the
self-learning property (the more an agent delivers on a metric, the higher it
ranks) instead of re-implementing ranking.

``capabilities`` is consumed as an **immutable sequence of capability slugs on
the agent spec/record** (the B2-capabilities-stream contract): :class:`CandidateAgent`
normalizes whatever sequence it is given into a frozen ``tuple`` so the roster a
caller passes in cannot mutate underneath the resolution. The store is injected
through ``scoreboard_fn`` (defaulting to the real goal store) so the resolver is
standalone-testable, mirroring the other omnigent-native stores' seams.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CandidateAgent:
    """A roster entry the resolver filters + ranks (BDP-2335).

    ``capabilities`` is an **immutable** tuple of capability slugs — the
    B2-capabilities-stream contract on the agent spec/record. ``department`` is
    optional so an unplaced agent is simply never matched by a department filter.
    """

    agent_id: str
    department: str | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Normalize whatever sequence the caller supplied into a frozen tuple so
        # the roster can't mutate under the resolution (mirrors the frozen-record
        # convention used by the other omnigent-native stores).
        if not isinstance(self.capabilities, tuple):
            object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def in_department(self, department: str) -> bool:
        return self.department == department


def _candidate(entry: CandidateAgent | object) -> CandidateAgent:
    """Coerce a roster entry into a :class:`CandidateAgent`.

    Accepts a :class:`CandidateAgent` as-is, or any object/spec/record exposing
    ``agent_id`` (or ``name``), ``department``, and a ``capabilities`` sequence —
    so a future B2 agent-spec/record can be threaded through unchanged.
    """
    if isinstance(entry, CandidateAgent):
        return entry
    agent_id = getattr(entry, "agent_id", None) or getattr(entry, "name", None)
    if not agent_id:
        raise ValueError("roster entry has no agent_id / name")
    raw_caps: Iterable[str] = getattr(entry, "capabilities", ()) or ()
    return CandidateAgent(
        agent_id=str(agent_id),
        department=getattr(entry, "department", None),
        capabilities=tuple(raw_caps),
    )


@dataclass(frozen=True)
class AssignmentResolution:
    """The outcome of resolving an assignee (BDP-2335).

    ``assignee`` is the resolved ``agent_id`` (or ``None`` if nothing qualifies).
    ``reason`` records which chain link decided it (``explicit`` / ``ranked`` /
    ``fallback`` / ``unassigned``). ``ranked`` is the eligible roster ordered by
    scoreboard merit (highest first) — exposed for transparency / audit.
    """

    assignee: str | None
    reason: str
    ranked: tuple[str, ...]


def _default_scoreboard(metric: str) -> list[tuple[str, float]]:
    """Reuse the find_specialist ranking step verbatim (the goal scoreboard)."""
    from bytedesk_omnigent.goals import get_goal_store

    # A large limit so ranking covers the whole eligible roster, not a top-N
    # slice that could drop an otherwise-eligible agent.
    return get_goal_store().scoreboard(metric=metric, limit=1000)


def resolve_assignee(
    *,
    metric: str,
    roster: Sequence[CandidateAgent | object],
    explicit_owner: str | None = None,
    capability: str | None = None,
    department: str | None = None,
    scoreboard_fn: Callable[[str], list[tuple[str, float]]] = _default_scoreboard,
) -> AssignmentResolution:
    """Resolve who should own a piece of work.

    Chain (first match wins): **explicit owner** → **(capability ∩ department)
    eligibility filter, then scoreboard rank** of the survivors.

    Filter on capability first, rank second — a high scorer who lacks the
    required ``capability`` (or sits outside ``department``) is never assigned.
    Among the eligible, order follows the scoreboard for ``metric`` (the same
    self-learning ranking ``find_specialist`` uses); eligible agents with no
    recorded score sort last (in stable roster order) so a freshly-onboarded
    agent is still assignable when no one has a track record yet.
    """
    candidates = [_candidate(e) for e in roster]

    # 1. Explicit owner wins — a deliberate choice is never overridden, and is
    #    honored even when that agent isn't in the (possibly partial) roster.
    if explicit_owner:
        return AssignmentResolution(
            assignee=explicit_owner, reason="explicit", ranked=(explicit_owner,)
        )

    # 2a. Eligibility filter: capability ∩ department. An absent filter is a
    #     no-op (does not exclude), so callers can require either, both, or none.
    eligible = [
        c
        for c in candidates
        if (capability is None or c.has_capability(capability))
        and (department is None or c.in_department(department))
    ]
    if not eligible:
        return AssignmentResolution(assignee=None, reason="unassigned", ranked=())

    # 2b. Merit rank: reuse the scoreboard verbatim. Build the score lookup, then
    #     order the eligible roster by score desc; unscored agents keep their
    #     stable roster order and sort after every scored agent.
    scores = dict(scoreboard_fn(metric))
    ranked = sorted(
        eligible,
        key=lambda c: (-(scores.get(c.agent_id, float("-inf"))),),
    )
    ranked_ids = tuple(c.agent_id for c in ranked)

    top = ranked[0]
    reason = "ranked" if top.agent_id in scores else "fallback"
    return AssignmentResolution(assignee=top.agent_id, reason=reason, ranked=ranked_ids)
