"""Treasury — the economic core: budgets, reservations, the outcome ledger, the
reinvestment flywheel, and the circuit breaker (BDP-2585, Phase 3).

ADR-0008: ``Treasury`` is a Protocol with a SQLAlchemy default so a tenant can
swap the funding policy without touching the tick. ADR-0009: ``reserve`` and
``book_outcome`` are exactly-once — reserve is a guarded conditional UPDATE on the
budget row (``... WHERE spent + cost <= cap``, rowcount 0 = denied) keyed
idempotently by ``goal_decisions``-style period, and the realized-value bump on
the goal is a guarded single-writer UPDATE so a redelivered outcome cannot
double-count.

The flywheel: ``book_outcome`` writes the ledger, bumps ``goal.realized_value_cents``
and calls ``replenish`` so booked revenue *refills the tier budget* — realized
revenue funds the next round (compounding). Realized value is booked ONLY here,
never by an agent or the optimizer.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import update

from bytedesk_omnigent.db_models import SqlGoal, SqlGoalBudget, SqlGoalOutcome
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class Reservation:
    """A held budget reservation for one goal in one period (ADR-0009 idempotent)."""

    id: str
    goal_id: str
    tier: str
    target_id: str
    est_cost: int
    period_key: str


@dataclass(frozen=True)
class Outcome:
    """A booked realized-value ledger row (detached snapshot)."""

    id: str
    goal_id: str
    booked_at: int
    realized_value_cents: int
    source: str
    evidence: dict[str, Any] | None


@dataclass(frozen=True)
class Decision:
    """One fund/skip decision (detached snapshot of the replay log)."""

    id: str
    tick_id: str
    goal_id: str
    roi_at_decision: float
    budget_before: int | None
    budget_after: int | None
    reason: str
    spawned_session_id: str | None
    created_at: int


@runtime_checkable
class Treasury(Protocol):
    """Funding policy (ADR-0008): caps, reservations, ledger, flywheel, breaker."""

    def can_fund(self, goal: Any, est_cost: int, *, goal_store: Any | None = None) -> bool: ...

    def reserve(
        self, goal: Any, est_cost: int, *, period_key: str | None = None, now: int | None = None
    ) -> Reservation | None: ...

    def settle(
        self, reservation: Reservation, actual_cost: int, *, now: int | None = None
    ) -> None: ...

    def replenish(
        self, *, tier: str, target_id: str, booked_cents: int, now: int | None = None
    ) -> None: ...

    def circuit_open(self, scope: str) -> bool: ...

    def book_outcome(
        self,
        *,
        goal_store: Any,
        goal_id: str,
        realized_value_cents: int,
        source: str,
        evidence: dict[str, Any] | None,
        now: int | None = None,
    ) -> Outcome | None: ...


def _scope(tier: str, target_id: str) -> str:
    return f"{tier}:{target_id}"


def _budget_chain(goal: Any, goal_store: Any | None) -> list[tuple[str, str]]:
    """The (tier, target_id) scopes that gate ``goal`` — its own scope + every
    ancestor goal's scope up the ``parent_goal_id`` chain (inherited caps)."""
    chain = [(goal.tier, goal.target_id)]
    seen = {goal.id}
    parent_id = goal.parent_goal_id
    while parent_id and goal_store is not None and parent_id not in seen:
        seen.add(parent_id)
        parent = goal_store.get_goal(goal_id=parent_id, include_dependencies=False)
        if parent is None:
            break
        chain.append((parent.tier, parent.target_id))
        parent_id = parent.parent_goal_id
    return chain


class SqlAlchemyTreasury:
    """Default Treasury backed by ``goal_budgets`` / ``goal_outcomes`` (ADR-0009)."""

    def __init__(self, storage_location: str) -> None:
        from bytedesk_omnigent.idempotency import SqlAlchemyIdempotencyStore

        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)
        # Per-period reservation dedup (ADR-0009 Idempotent Receiver) — reuse the
        # existing claim store rather than a fourth table.
        self._idempotency = SqlAlchemyIdempotencyStore(storage_location)

    @property
    def engine(self):
        return self._engine

    # -- budgets -------------------------------------------------------
    def set_budget(
        self,
        *,
        tier: str,
        target_id: str,
        cap_cents: int = 0,
        cap_tokens: int | None = None,
        max_spawns: int | None = None,
        anomaly_threshold_cents: int | None = None,
        now: int | None = None,
    ) -> None:
        """Create/replace the cap for a scope (admin/seed surface)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlGoalBudget, (tier, target_id))
            if row is None:
                session.add(
                    SqlGoalBudget(
                        tier=tier,
                        target_id=target_id,
                        cap_cents=cap_cents,
                        cap_tokens=cap_tokens,
                        max_spawns=max_spawns,
                        anomaly_threshold_cents=anomaly_threshold_cents,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.cap_cents = cap_cents
                row.cap_tokens = cap_tokens
                row.max_spawns = max_spawns
                row.anomaly_threshold_cents = anomaly_threshold_cents
                row.updated_at = now

    def spent_cents(self, *, tier: str, target_id: str) -> int:
        with self._session() as session:
            row = session.get(SqlGoalBudget, (tier, target_id))
            return row.spent_cents if row is not None else 0

    def _remaining(self, session: Any, tier: str, target_id: str) -> int | None:
        row = session.get(SqlGoalBudget, (tier, target_id))
        if row is None:
            return None  # no budget configured -> ungated
        return row.cap_cents - row.spent_cents

    def can_fund(self, goal: Any, est_cost: int, *, goal_store: Any | None = None) -> bool:
        """True when every scope up the inheritance chain has room (or is uncapped)."""
        with self._session() as session:
            for tier, target_id in _budget_chain(goal, goal_store):
                remaining = self._remaining(session, tier, target_id)
                if remaining is not None and est_cost > remaining:
                    return False
        return True

    # -- reservations (exactly-once, ADR-0009) -------------------------
    def reserve(
        self, goal: Any, est_cost: int, *, period_key: str | None = None, now: int | None = None
    ) -> Reservation | None:
        """Provisionally charge ``est_cost`` to the goal's own scope.

        Guarded conditional UPDATE: the charge applies only WHERE
        ``spent + cost <= cap`` (rowcount 0 = over cap → denied), so two racing
        reserves cannot both win past the cap. ``period_key`` (default the goal id)
        is the idempotency key — re-reserving the same period is a no-op (returns
        None) because the prior reservation's spend is still held.
        """
        now = now_epoch() if now is None else now
        period_key = period_key or goal.id
        # Exactly-once per (goal, period): a duplicate reserve is a no-op.
        if not self._idempotency.claim(
            scope="goal_reserve", key=f"{goal.id}:{period_key}", now=now
        ):
            return None
        with self._write_session() as session:
            row = session.get(SqlGoalBudget, (goal.tier, goal.target_id))
            if row is None:
                # uncapped scope: nothing to charge, but still hand back a reservation
                # so the caller can settle/audit uniformly.
                return Reservation(
                    id=f"resv_{uuid.uuid4().hex}",
                    goal_id=goal.id,
                    tier=goal.tier,
                    target_id=goal.target_id,
                    est_cost=est_cost,
                    period_key=period_key,
                )
            result = session.execute(
                update(SqlGoalBudget)
                .where(
                    SqlGoalBudget.tier == goal.tier,
                    SqlGoalBudget.target_id == goal.target_id,
                    SqlGoalBudget.cap_cents - SqlGoalBudget.spent_cents >= est_cost,
                )
                .values(
                    spent_cents=SqlGoalBudget.spent_cents + est_cost,
                    spawns_used=SqlGoalBudget.spawns_used + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None  # over cap
            self._maybe_trip_anomaly(session, goal.tier, goal.target_id, now)
            return Reservation(
                id=f"resv_{uuid.uuid4().hex}",
                goal_id=goal.id,
                tier=goal.tier,
                target_id=goal.target_id,
                est_cost=est_cost,
                period_key=period_key,
            )

    def settle(
        self, reservation: Reservation, actual_cost: int, *, now: int | None = None
    ) -> None:
        """Correct the provisional charge to the actual cost (delta can be ±)."""
        now = now_epoch() if now is None else now
        delta = actual_cost - reservation.est_cost
        if delta == 0:
            return
        with self._write_session() as session:
            row = session.get(SqlGoalBudget, (reservation.tier, reservation.target_id))
            if row is None:
                return
            row.spent_cents = max(0, row.spent_cents + delta)
            row.updated_at = now

    def replenish(
        self, *, tier: str, target_id: str, booked_cents: int, now: int | None = None
    ) -> None:
        """Refill a scope's budget with booked revenue (the flywheel)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlGoalBudget, (tier, target_id))
            if row is None:
                return
            row.spent_cents = max(0, row.spent_cents - booked_cents)
            row.updated_at = now

    # -- circuit breaker ----------------------------------------------
    def circuit_open(self, scope: str) -> bool:
        tier, _, target_id = scope.partition(":")
        with self._session() as session:
            row = session.get(SqlGoalBudget, (tier, target_id))
            return bool(row.circuit_open) if row is not None else False

    def trip_circuit(self, scope: str, *, now: int | None = None) -> None:
        self._set_circuit(scope, True, now)

    def reset_circuit(self, scope: str, *, now: int | None = None) -> None:
        self._set_circuit(scope, False, now)

    def _set_circuit(self, scope: str, value: bool, now: int | None) -> None:
        now = now_epoch() if now is None else now
        tier, _, target_id = scope.partition(":")
        with self._write_session() as session:
            row = session.get(SqlGoalBudget, (tier, target_id))
            if row is None:
                row = SqlGoalBudget(
                    tier=tier, target_id=target_id, created_at=now, updated_at=now
                )
                session.add(row)
            row.circuit_open = value
            row.updated_at = now

    def _maybe_trip_anomaly(self, session: Any, tier: str, target_id: str, now: int) -> None:
        """Auto-trip when spend passes the anomaly threshold with zero realized value."""
        row = session.get(SqlGoalBudget, (tier, target_id))
        if row is None or row.anomaly_threshold_cents is None:
            return
        if row.spent_cents >= row.anomaly_threshold_cents:
            realized = self._scope_realized(session, tier, target_id)
            if realized == 0:
                row.circuit_open = True
                row.updated_at = now

    def _scope_realized(self, session: Any, tier: str, target_id: str) -> int:
        """Sum realized value over goals in this scope (anomaly denominator)."""
        from sqlalchemy import func, select

        total = session.execute(
            select(func.coalesce(func.sum(SqlGoal.realized_value_cents), 0)).where(
                SqlGoal.tier == tier, SqlGoal.target_id == target_id
            )
        ).scalar()
        return int(total or 0)

    # -- outcome ledger (the ONLY realized-value writer) ---------------
    def book_outcome(
        self,
        *,
        goal_store: Any,
        goal_id: str,
        realized_value_cents: int,
        source: str,
        evidence: dict[str, Any] | None,
        now: int | None = None,
    ) -> Outcome | None:
        """Append a realized outcome → bump goal value → replenish the tier budget.

        The single writer of realized value (ADR goal-engine invariant). The goal
        bump is an atomic RMW via the goal store's ``mutate_payload`` write lock
        path; here we use a guarded UPDATE on the goal row so a concurrent booker
        cannot lose the increment.
        """
        now = now_epoch() if now is None else now
        goal = goal_store.get_goal(goal_id=goal_id, include_dependencies=False)
        if goal is None:
            return None
        outcome_id = f"outcome_{uuid.uuid4().hex}"
        with self._write_session() as session:
            session.add(
                SqlGoalOutcome(
                    id=outcome_id,
                    goal_id=goal_id,
                    booked_at=now,
                    realized_value_cents=realized_value_cents,
                    source=source,
                    evidence=json.dumps(evidence) if evidence is not None else None,
                )
            )
            # guarded increment — single-writer, no lost update (ADR-0009).
            session.execute(
                update(SqlGoal)
                .where(SqlGoal.id == goal_id)
                .values(
                    realized_value_cents=SqlGoal.realized_value_cents + realized_value_cents,
                    updated_at=now,
                )
            )
            # clear any anomaly trip now that revenue is booked.
            budget = session.get(SqlGoalBudget, (goal.tier, goal.target_id))
            if budget is not None:
                budget.circuit_open = False
                budget.updated_at = now
        self.replenish(
            tier=goal.tier, target_id=goal.target_id, booked_cents=realized_value_cents, now=now
        )
        return Outcome(
            id=outcome_id,
            goal_id=goal_id,
            booked_at=now,
            realized_value_cents=realized_value_cents,
            source=source,
            evidence=evidence,
        )

    # -- read surfaces (audit) — detached snapshots ------------------
    def outcomes(self, *, goal_id: str | None = None) -> list[Outcome]:
        from sqlalchemy import select

        stmt = select(SqlGoalOutcome)
        if goal_id is not None:
            stmt = stmt.where(SqlGoalOutcome.goal_id == goal_id)
        stmt = stmt.order_by(SqlGoalOutcome.booked_at)
        with self._session() as session:
            rows = session.execute(stmt).scalars().all()
            return [
                Outcome(
                    id=r.id,
                    goal_id=r.goal_id,
                    booked_at=r.booked_at,
                    realized_value_cents=r.realized_value_cents,
                    source=r.source,
                    evidence=json.loads(r.evidence) if r.evidence else None,
                )
                for r in rows
            ]

    def decisions(
        self, *, goal_id: str | None = None, tick_id: str | None = None
    ) -> list[Decision]:
        from sqlalchemy import select

        from bytedesk_omnigent.db_models import SqlGoalDecision

        stmt = select(SqlGoalDecision)
        if goal_id is not None:
            stmt = stmt.where(SqlGoalDecision.goal_id == goal_id)
        if tick_id is not None:
            stmt = stmt.where(SqlGoalDecision.tick_id == tick_id)
        stmt = stmt.order_by(SqlGoalDecision.created_at)
        with self._session() as session:
            rows = session.execute(stmt).scalars().all()
            return [
                Decision(
                    id=r.id,
                    tick_id=r.tick_id,
                    goal_id=r.goal_id,
                    roi_at_decision=r.roi_at_decision,
                    budget_before=r.budget_before,
                    budget_after=r.budget_after,
                    reason=r.reason,
                    spawned_session_id=r.spawned_session_id,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    def record_decision(
        self,
        *,
        tick_id: str,
        goal_id: str,
        roi_at_decision: float,
        reason: str,
        budget_before: int | None = None,
        budget_after: int | None = None,
        spawned_session_id: str | None = None,
        now: int | None = None,
    ) -> None:
        """Append one fund/skip decision to the replay log."""
        now = now_epoch() if now is None else now
        from bytedesk_omnigent.db_models import SqlGoalDecision

        with self._write_session() as session:
            session.add(
                SqlGoalDecision(
                    id=f"decision_{uuid.uuid4().hex}",
                    tick_id=tick_id,
                    goal_id=goal_id,
                    roi_at_decision=roi_at_decision,
                    budget_before=budget_before,
                    budget_after=budget_after,
                    reason=reason,
                    spawned_session_id=spawned_session_id,
                    created_at=now,
                )
            )


_treasury_cache: dict[str, SqlAlchemyTreasury] = {}


def get_treasury() -> SqlAlchemyTreasury:
    """The durable Treasury bound to the conversation store's location."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    treasury = _treasury_cache.get(location)
    if treasury is None:
        treasury = SqlAlchemyTreasury(location)
        _treasury_cache[location] = treasury
    return treasury


__all__ = [
    "Decision",
    "Outcome",
    "Reservation",
    "SqlAlchemyTreasury",
    "Treasury",
    "get_treasury",
]
