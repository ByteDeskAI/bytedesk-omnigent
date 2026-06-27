"""Goal-engine tick + background loop (BDP-2583, ADR-0142).

A sibling of the cron-scheduler and accountability loops: every
``interval_seconds`` it scans **ready, immediate, owned** goals and dispatches
each that has no live session yet (one session per goal, ADR-0009 idempotent via
``dispatch_goal``'s unique ``external_key``). Recurring / until_done goals are NOT
handled here — they run off their cron trigger (``engine.cron``). A re-tick is a
no-op once a goal has its live session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from bytedesk_omnigent.engine.dispatcher import ConversationSpawnPort, dispatch_goal
from bytedesk_omnigent.goals import roi
from bytedesk_omnigent.maintenance import advisory_locked_loop

_logger = logging.getLogger(__name__)

# Default estimated cost (cents) of one agent turn when a caller doesn't supply one.
# ponytail: flat estimate; per-agent/per-model cost model is Phase 6.
_DEFAULT_EST_COST = 100

# Stable 64-bit advisory-lock key for the goal engine ("goalengn") — distinct from
# the cron, accountability, signal-bus, and outbox keys so the sweeps never contend.
_GOAL_ENGINE_LOCK_KEY = 0x676F616C656E676E

_DEFAULT_INTERVAL_SECONDS = 30


def run_goal_engine_tick(
    goal_store,
    conversation_store: ConversationSpawnPort,
    *,
    now: int | None = None,
    sensor_registry=None,
    treasury=None,
    optimizer=None,
    est_cost: int = _DEFAULT_EST_COST,
) -> int:
    """Dispatch ready, immediate, owned ``assigned`` goals. Returns count spawned.

    A goal becomes workable once it is *claimed* (``assigned`` + an owner). The
    tick spawns its working session; ``dispatch_goal``'s unique session key makes a
    re-tick a no-op. Injectable stores so the tick is unit-provable without a live
    runner.

    BDP-2584 (additive): when ``sensor_registry`` is provided, each candidate is
    gated through ``engine.resolver.resolve``.

    BDP-2585 (Phase 3 — the portfolio optimizer): when **both** ``treasury`` and
    ``optimizer`` are provided the tick stops being first-come dispatch and becomes
    a ROI portfolio optimizer — it ranks the actionable frontier, then funds
    top-down within budget: skip if the scope's circuit is open; skip if not
    fundable; route a high-risk goal to approval instead of spawning; *simulate* a
    paper-trading goal (no real session, no booked value); else reserve → dispatch
    → record the decision. Finally it rolls each goal's realized value up to its
    parent. With neither injected the behaviour is exactly Phase 2.
    """
    candidates = [
        g
        for g in goal_store.list_goals(
            status="assigned",
            activation_state="ready",
            include_dependencies=sensor_registry is not None,
        )
        if g.cadence_kind == "immediate" and g.owner_agent_id
    ]
    if sensor_registry is not None:
        from bytedesk_omnigent.engine.resolver import resolve

        candidates = [
            g
            for g in candidates
            if resolve(g, registry=sensor_registry, goal_store=goal_store, now=now or 0)[
                "actionable"
            ]
        ]

    if treasury is None or optimizer is None:
        # Phase 2 path: first-come dispatch, no economics gating.
        spawned = 0
        for goal in candidates:
            if dispatch_goal(
                goal, conversation_store=conversation_store, goal_store=goal_store, now=now
            ).spawned:
                spawned += 1
        return spawned

    return _run_portfolio_tick(
        candidates,
        goal_store=goal_store,
        conversation_store=conversation_store,
        treasury=treasury,
        optimizer=optimizer,
        est_cost=est_cost,
        now=now,
    )


def _run_portfolio_tick(
    candidates,
    *,
    goal_store,
    conversation_store: ConversationSpawnPort,
    treasury,
    optimizer,
    est_cost: int,
    now: int | None,
) -> int:
    """The economic core (BDP-2585): rank → fund top-down within budget → roll up."""
    tick_id = uuid.uuid4().hex
    ranked = optimizer.rank(candidates, now=now or 0)
    spawned = 0
    for goal in ranked:
        scope = f"{goal.tier}:{goal.target_id}"
        goal_roi = roi(goal, remaining_budget_cents=max(est_cost, 1))

        if treasury.circuit_open(scope):
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="skip_circuit_open", now=now,
            )
            continue
        if not treasury.can_fund(goal, est_cost, goal_store=goal_store):
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="skip_no_budget", now=now,
            )
            continue
        if goal.attributes.get("paper_trading"):
            # Simulate: charge nothing real, book nothing, just record the decision.
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="paper_trade", now=now,
            )
            continue
        if goal.risk_tier == "high":
            # Blast-radius gate: high-risk routes to approval, never auto-spawns
            # (approval UI is Phase 6 — here we gate + mark + record).
            goal_store.mutate_payload(
                goal_id=goal.id,
                mutator=lambda p: p.setdefault("attributes", {}).update(
                    {"approval_state": "pending"}
                ),
                now=now,
            )
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="approval_required", now=now,
            )
            continue

        reservation = treasury.reserve(goal, est_cost, now=now)
        if reservation is None:
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="skip_no_budget", now=now,
            )
            continue
        result = dispatch_goal(
            goal, conversation_store=conversation_store, goal_store=goal_store, now=now
        )
        if result.spawned:
            spawned += 1
        treasury.record_decision(
            tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
            reason="funded", spawned_session_id=result.session_id, now=now,
        )

    _roll_up_child_outcomes(goal_store, now=now)
    return spawned


def _roll_up_child_outcomes(goal_store, *, now: int | None) -> None:
    """Set each parent's realized value to the sum of its children's (ADR-0154).

    Generalizes the Epic auto-complete roll-up. ``book_outcome`` owns *leaf*
    realized value; this only aggregates it upward. Idempotent — a parent with no
    children-value-change is left untouched. A parent that also has its own direct
    outcomes is out of scope (children-only roll-up).
    """
    from sqlalchemy import update as _update

    from bytedesk_omnigent.db_models import SqlGoal
    from omnigent.db.utils import now_epoch

    by_parent: dict[str, int] = {}
    for g in goal_store.list_goals():
        if g.parent_goal_id:
            by_parent[g.parent_goal_id] = (
                by_parent.get(g.parent_goal_id, 0) + g.realized_value_cents
            )
    ts = now_epoch() if now is None else now
    for parent_id, child_total in by_parent.items():
        parent = goal_store.get_goal(goal_id=parent_id, include_dependencies=False)
        if parent is None or parent.realized_value_cents == child_total:
            continue
        with goal_store._write_session() as session:
            session.execute(
                _update(SqlGoal)
                .where(SqlGoal.id == parent_id)
                .values(realized_value_cents=child_total, updated_at=ts)
            )


async def goal_engine_loop(
    *,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    lock_key: int = _GOAL_ENGINE_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` dispatch ready immediate goals.

    Guarded by a distinct PG advisory lock (no-op on SQLite). Resilient — a failed
    tick is logged and the loop continues; cancellation propagates for clean
    shutdown. Blocking DB work runs in a worker thread.
    """
    from bytedesk_omnigent.engine.optimizer import RoiOptimizer
    from bytedesk_omnigent.engine.sensors import build_default_registry
    from bytedesk_omnigent.engine.treasury import get_treasury
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    # BDP-2584: activate the condition resolver in production — each candidate is
    # gated through its sensor conditions (legacy no-AST goals resolve identically
    # to _activation_for, so this is behaviour-preserving for existing goals).
    sensor_registry = build_default_registry()
    # BDP-2585: activate the portfolio optimizer in production. Behaviour-preserving
    # for legacy goals — an EV=0 goal in an uncapped scope is funded as before
    # (can_fund True, no circuit, low risk), just ranked + decision-logged.
    optimizer = RoiOptimizer()

    def _prepare():
        goal_store = get_goal_store()
        conversation_store = get_conversation_store()
        treasury = get_treasury()

        async def _work() -> None:
            spawned = await asyncio.to_thread(
                run_goal_engine_tick,
                goal_store,
                conversation_store,
                sensor_registry=sensor_registry,
                treasury=treasury,
                optimizer=optimizer,
            )
            if spawned:
                _logger.info("goal engine: spawned=%d", spawned)

        return goal_store.engine, _work

    await advisory_locked_loop(
        interval_seconds=interval_seconds,
        lock_key=lock_key,
        prepare=_prepare,
        logger=_logger,
        name="goal engine",
    )
