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

# Fallback estimated cost (cents) of one agent turn when a goal names no model.
# BDP-2594: a goal that declares payload['model'] is priced per-model via
# engine.cost; this flat value covers legacy goals with no model hint.
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
    config=None,
    configs=None,
    roster_provider=None,
    actuator_registry=None,
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

    BDP-2589 (Phase 7 — autonomy posture): ``config`` is the default
    :class:`~bytedesk_omnigent.engine.config.GoalEngineConfig` and ``configs`` an
    optional ``{target_id: GoalEngineConfig}`` override map (per-tenant). Under the
    DEFAULT ``gated`` posture, behaviour is unchanged unless the tenant set
    ``require_approval_all`` (then every funded goal routes to approval). Under
    ``full_auto`` a within-budget goal funds+spawns with no per-action approval —
    but the blast-radius gate (high-risk + ``high_risk_required``) ALWAYS holds, so
    full_auto never auto-spawns a high-risk goal. ``config=None`` preserves today's
    behaviour (gated, high-risk gated).

    BDP-2594 (Phase 2 Wave 1 — close the loop), all additive + behaviour-preserving
    when the new seams are not injected:

    - ``roster_provider`` (``() -> [CandidateAgent]``): when provided, unowned ready
      ``open`` goals are routed through the ``goal_assignment`` seam, claimed, and
      become dispatch candidates. No provider → unowned goals are left untouched.
    - ``sensor_registry`` also drives **success-condition auto-completion**: any
      non-``done`` goal carrying a ``success_condition`` whose tree is satisfied is
      advanced to ``done`` (idempotent) — "done is evaluated, not declared".
    - ``actuator_registry``: a goal that declares ``payload['actuator']`` is executed
      deterministically (no agent spawn) and advanced to ``done`` on success.
    - ``est_cost`` is now a per-goal estimate (model price × tokens) and the goal's
      reservation is settled to its actual cost when it completes.
    """
    # BDP-2594: assignment pre-pass — claim an owner for unowned ready open goals so
    # they enter the candidate frontier (no-op without a roster_provider).
    if roster_provider is not None:
        _assign_unowned_goals(goal_store, now=now, roster_provider=roster_provider)

    # BDP-2594: success-condition auto-completion + deterministic actuator path run
    # ahead of dispatch — a goal that is already complete or actuator-satisfiable
    # should not also spawn a working session this tick.
    if sensor_registry is not None:
        _complete_satisfied_goals(
            goal_store, now=now, sensor_registry=sensor_registry, treasury=treasury,
        )
    if actuator_registry is not None:
        _run_actuator_goals(
            goal_store, now=now, actuator_registry=actuator_registry, treasury=treasury,
        )

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
        config=config,
        configs=configs or {},
    )


def _ready_tenant_ids(goal_store) -> set[str]:
    """Distinct ``target_id`` of the ready, immediate, owned frontier (BDP-2589).

    Used by the loop to resolve a per-tenant config for each tenant present —
    cheap (it reads the same rows the tick will), so per-tenant posture works
    without a config lookup per goal inside the sync tick.
    """
    return {
        g.target_id
        for g in goal_store.list_goals(status="assigned", activation_state="ready")
        if g.cadence_kind == "immediate" and g.owner_agent_id
    }


def _posture_for(goal, config, configs):
    """Resolve the effective config for ``goal`` (per-tenant override → default).

    ``None`` means no Phase-7 config injected → today's gated behaviour.
    """
    if configs:
        per_tenant = configs.get(goal.target_id)
        if per_tenant is not None:
            return per_tenant
    return config


# -- BDP-2594 close-the-loop helpers ---------------------------------------


def _goal_cost(goal, est_cost: int) -> int:
    """Per-goal reservation cost: model-priced when the goal names a model, else
    the caller's flat ``est_cost`` (behaviour-preserving for legacy goals)."""
    payload = goal.payload or {}
    if isinstance(payload, dict) and payload.get("model"):
        from bytedesk_omnigent.engine.cost import goal_est_cost_cents

        return goal_est_cost_cents(goal)
    return est_cost


def _ensure_scope_budget(treasury, goal, cfg) -> None:
    """Wire dormant config knobs onto the goal's own budget scope (BDP-2594).

    When the scope has no budget row and ``budget_default_cap_cents > 0``, seed it
    with that cap. When ``anomaly_threshold_cents`` is set, ensure the row carries
    it so the treasury's auto-trip is live. No-op when knobs are 0/unset, and never
    lowers an existing explicit cap.
    """
    if cfg is None:
        return
    cap = getattr(cfg, "budget_default_cap_cents", 0) or 0
    threshold = getattr(cfg, "anomaly_threshold_cents", None)
    if cap <= 0 and not threshold:
        return
    set_budget = getattr(treasury, "set_budget", None)
    if set_budget is None:
        return
    existing = _scope_budget_row(treasury, goal)
    if existing is None:
        set_budget(
            tier=goal.tier, target_id=goal.target_id,
            cap_cents=cap, anomaly_threshold_cents=threshold,
        )
        return
    # Row exists: only add an anomaly threshold if it's missing (don't touch caps).
    if threshold and existing.anomaly_threshold_cents is None:
        set_budget(
            tier=goal.tier, target_id=goal.target_id,
            cap_cents=existing.cap_cents, cap_tokens=existing.cap_tokens,
            max_spawns=existing.max_spawns, anomaly_threshold_cents=threshold,
        )


def _scope_budget_row(treasury, goal):
    """The goal scope's budget row, or None (best-effort; SqlAlchemyTreasury only)."""
    engine = getattr(treasury, "engine", None)
    if engine is None:
        return None
    from sqlalchemy.orm import Session

    from bytedesk_omnigent.db_models import SqlGoalBudget

    with Session(engine) as session:
        return session.get(SqlGoalBudget, (goal.tier, goal.target_id))


def _settle_goal(goal, treasury) -> None:
    """Settle a completed goal's reservation to its actual cost (BDP-2594).

    Reconstructs the in-memory-only :class:`Reservation` from the goal's scope +
    the estimate it reserved (stored on the payload at reserve time), then settles
    to the goal's actual cost. ``settle`` is a delta UPDATE — a zero delta is a
    no-op, so a goal that completed without a real measured cost (the common case)
    settles cleanly to the estimate.
    """
    if treasury is None or not hasattr(treasury, "settle"):
        return
    from bytedesk_omnigent.engine.cost import actual_cost_cents
    from bytedesk_omnigent.engine.treasury import Reservation

    reserved = goal.attributes.get("reserved_cost_cents")
    if not isinstance(reserved, int):
        return  # this goal was never funded through the treasury — nothing to settle.
    actual = actual_cost_cents(goal, fallback=reserved)
    treasury.settle(
        Reservation(
            id="", goal_id=goal.id, tier=goal.tier, target_id=goal.target_id,
            est_cost=reserved, period_key=goal.id,
        ),
        actual,
    )


def _assign_unowned_goals(goal_store, *, now, roster_provider) -> None:
    """Claim an owner for unowned ready ``open`` goals via the assignment seam.

    Resolves each via the ``goal_assignment`` registry (capability ∩ department +
    scoreboard, ``assignment.resolve_assignee``) over the provided roster, then
    ``claim_goal`` to assign it. No candidate → the goal is left ``open`` (waiting)
    with no crash. The roster excludes system/workflow agents by construction (the
    provider supplies employee-tier candidates).
    """
    from bytedesk_omnigent.engine.registries import build_assignment_registry

    unowned = [
        g
        for g in goal_store.list_goals(status="open", activation_state="ready")
        if g.cadence_kind == "immediate" and not g.owner_agent_id
    ]
    if not unowned:
        return
    roster = roster_provider()
    if not roster:
        return
    policy = build_assignment_registry().resolve_default()
    for goal in unowned:
        hints = (goal.payload or {}).get("assignment") if isinstance(goal.payload, dict) else None
        hints = hints if isinstance(hints, dict) else {}
        resolution = policy.resolve_assignee(
            metric=hints.get("metric", "goal"),
            roster=roster,
            capability=hints.get("capability"),
            department=hints.get("department"),
            scoreboard_fn=lambda metric: goal_store.scoreboard(metric=metric, limit=1000),
        )
        if resolution.assignee:
            goal_store.claim_goal(goal_id=goal.id, owner_agent_id=resolution.assignee, now=now)


def _complete_satisfied_goals(goal_store, *, now, sensor_registry, treasury) -> None:
    """Advance non-done goals whose ``success_condition`` is satisfied to ``done``.

    "Done is evaluated, not declared" (BDP-2594). Idempotent: only ``assigned`` /
    ``in_progress`` goals are evaluated (a ``done`` goal is skipped, so a re-tick
    never hits the terminal ``done -> done`` transition). Realized value is NOT
    fabricated here — a rail's ``book_outcome`` owns that.
    """
    from bytedesk_omnigent.engine.resolver import evaluate_success_condition

    for status in ("assigned", "in_progress"):
        for goal in goal_store.list_goals(status=status):
            if goal.success_condition is None:
                continue
            if evaluate_success_condition(
                goal, registry=sensor_registry, goal_store=goal_store, now=now or 0
            ):
                goal_store.advance_goal(goal_id=goal.id, status="done", now=now)
                _settle_goal(goal, treasury)


def _run_actuator_goals(goal_store, *, now, actuator_registry, treasury) -> None:
    """Execute goals that declare a deterministic actuator (no agent spawn, BDP-2594).

    A goal with ``payload['actuator'] = {name, input}`` is run via the
    ``goal_actuator`` seam instead of spawning an agent: resolve the actuator,
    ``execute(input)``, and advance to ``done`` on ``ok``. A failed actuator leaves
    the goal as-is (a later tick / agent can retry).

    ponytail: deterministic path only. Exposing actuators to a SPAWNED agent as MCP
    tools is heavier — deferred to a later wave; the seam + this hook are the upgrade
    point.
    """
    import asyncio

    for status in ("assigned", "in_progress"):
        for goal in goal_store.list_goals(status=status):
            payload = goal.payload or {}
            spec = payload.get("actuator") if isinstance(payload, dict) else None
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            try:
                actuator = actuator_registry.get(spec["name"])
            except Exception:  # noqa: BLE001 - an unknown actuator leaves the goal for retry
                _logger.warning("goal %s names unknown actuator %r", goal.id, spec["name"])
                continue
            result = asyncio.run(actuator.execute(spec.get("input", {})))
            if result.ok:
                goal_store.advance_goal(goal_id=goal.id, status="done", now=now)
                _settle_goal(goal, treasury)


def _run_portfolio_tick(
    candidates,
    *,
    goal_store,
    conversation_store: ConversationSpawnPort,
    treasury,
    optimizer,
    est_cost: int,
    now: int | None,
    config=None,
    configs=None,
) -> int:
    """The economic core (BDP-2585): rank → fund top-down within budget → roll up.

    BDP-2589: applies the per-goal autonomy posture (``config``/``configs``).
    """
    tick_id = uuid.uuid4().hex
    ranked = optimizer.rank(candidates, now=now or 0)
    spawned = 0
    for goal in ranked:
        scope = f"{goal.tier}:{goal.target_id}"
        cfg = _posture_for(goal, config, configs)
        # BDP-2594: per-goal cost = the model-priced estimate when the goal names a
        # model, else the caller's flat est_cost (behaviour-preserving for legacy).
        goal_cost = _goal_cost(goal, est_cost)
        goal_roi = roi(goal, remaining_budget_cents=max(goal_cost, 1))
        # BDP-2594: wire the dormant config knobs onto the goal's budget scope
        # (auto-seed a default cap / anomaly threshold). No-op when unset/0.
        _ensure_scope_budget(treasury, goal, cfg)

        if treasury.circuit_open(scope):
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="skip_circuit_open", now=now,
            )
            continue
        if not treasury.can_fund(goal, goal_cost, goal_store=goal_store):
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
        # Blast-radius gate (ALWAYS, in either posture): a high-risk goal routes to
        # approval and never auto-spawns. ``high_risk_required`` defaults True; only
        # an explicit per-tenant opt-out (gated posture) relaxes it.
        high_risk_gated = goal.risk_tier == "high" and (
            cfg is None or cfg.high_risk_required
        )
        # Per-tenant 'approve everything' (gated posture only): route every funded,
        # non-high-risk goal to approval too. full_auto bypasses this (it is the arm).
        gated_all = (
            cfg is not None
            and cfg.autonomy_posture == "gated"
            and cfg.require_approval_all
        )
        if high_risk_gated or gated_all:
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

        reservation = treasury.reserve(goal, goal_cost, now=now)
        if reservation is None:
            treasury.record_decision(
                tick_id=tick_id, goal_id=goal.id, roi_at_decision=goal_roi,
                reason="skip_no_budget", now=now,
            )
            continue
        # Remember the reserved estimate on the goal so completion can settle the
        # exact delta later (the reservation object is in-memory only).
        goal_store.mutate_payload(
            goal_id=goal.id,
            mutator=lambda p, c=goal_cost: p.setdefault("attributes", {}).update(
                {"reserved_cost_cents": c}
            ),
            now=now,
        )
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


def _employee_roster():
    """Build the assignment roster from employee-tier agents (BDP-2594).

    Tier-aware by construction: ``list(category="employee")`` excludes system and
    workflow agents, so the assignment seam never routes a goal to an orchestrator
    or a system automation. Each candidate carries its declared capabilities;
    department enrichment is a later seam (an absent department simply never matches
    a department-filtered goal). Best-effort — a roster build error yields an empty
    roster (no assignment this tick) rather than crashing the loop.
    """
    from bytedesk_omnigent.assignment import CandidateAgent

    try:
        from omnigent.runtime import get_agent_store

        store = get_agent_store()
        roster: list[CandidateAgent] = []
        after: str | None = None
        while True:
            page = store.list(limit=100, after=after, category="employee")
            for agent in page.data:
                try:
                    caps = store.get_capabilities(agent.id)
                except Exception:  # noqa: BLE001 - enrichment must never crash the roster
                    caps = ()
                roster.append(CandidateAgent(agent_id=agent.id, capabilities=tuple(caps)))
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return roster
    except Exception:  # noqa: BLE001 - runtime may be uninitialized; degrade to no assignment
        _logger.debug("employee roster unavailable; skipping assignment this tick")
        return []


async def goal_engine_loop(
    *,
    interval_seconds: int | None = None,
    lock_key: int = _GOAL_ENGINE_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` dispatch ready immediate goals.

    Guarded by a distinct PG advisory lock (no-op on SQLite). Resilient — a failed
    tick is logged and the loop continues; cancellation propagates for clean
    shutdown. Blocking DB work runs in a worker thread.

    BDP-2589: when ``interval_seconds`` is not passed, the global
    ``goals.tick.interval_seconds`` flag sets the cadence (hot-reloadable on the
    next loop restart; default 30s). An explicit caller value always wins.
    """
    from bytedesk_omnigent.engine.config import load_goal_engine_config
    from bytedesk_omnigent.engine.registries import (
        build_optimizer_registry,
        build_treasury_registry,
    )
    from bytedesk_omnigent.engine.sensors import build_default_registry
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    # BDP-2584: activate the condition resolver in production — each candidate is
    # gated through its sensor conditions (legacy no-AST goals resolve identically
    # to _activation_for, so this is behaviour-preserving for existing goals).
    sensor_registry = build_default_registry()
    # BDP-2589: resolve the portfolio strategies through the pluggable seams (ADR-0008)
    # so a tenant/operator can swap the optimizer/treasury via OMNIGENT_USE_<SEAM>
    # without forking. Defaults are the Phase-3/5 built-ins, so behaviour is preserved.
    # The global config's risk-decay tunes the default RoiOptimizer (ranking is global).
    _global_config = await load_goal_engine_config(None)
    optimizer = build_optimizer_registry().resolve_default()
    if type(optimizer).__name__ == "RoiOptimizer":
        optimizer = type(optimizer)(risk_decay=_global_config.risk_decay)

    # BDP-2594: the deterministic actuator seam — built once, extensions discovered.
    # A goal that declares ``payload['actuator']`` is executed here (no agent spawn).
    from bytedesk_omnigent.engine.providers.contract import ActuatorRegistry

    actuator_registry = ActuatorRegistry()
    try:
        actuator_registry.discover_actuator_extensions()
    except Exception:  # noqa: BLE001 - no contributed actuators is the common case
        _logger.debug("no goal actuators discovered")

    def _prepare():
        goal_store = get_goal_store()
        conversation_store = get_conversation_store()
        treasury = build_treasury_registry(
            get_conversation_store().storage_location
        ).resolve_default()

        async def _work() -> None:
            # BDP-2589: resolve the per-tenant autonomy posture/config before the
            # sync tick. The global (tenant-less) config is the default; each tenant
            # present in the ready frontier gets its own resolved config. Defaults are
            # SAFE (gated), so an unseeded flag store preserves today's behaviour.
            default_config = await load_goal_engine_config(None)
            tenant_ids = await asyncio.to_thread(_ready_tenant_ids, goal_store)
            configs = {
                tid: await load_goal_engine_config(tid) for tid in sorted(tenant_ids)
            }
            spawned = await asyncio.to_thread(
                run_goal_engine_tick,
                goal_store,
                conversation_store,
                sensor_registry=sensor_registry,
                treasury=treasury,
                optimizer=optimizer,
                config=default_config,
                configs=configs,
                roster_provider=lambda: _employee_roster(),
                actuator_registry=actuator_registry,
            )
            if spawned:
                _logger.info("goal engine: spawned=%d", spawned)

        return goal_store.engine, _work

    if interval_seconds is None:
        interval_seconds = _global_config.tick_interval_seconds

    await advisory_locked_loop(
        interval_seconds=interval_seconds,
        lock_key=lock_key,
        prepare=_prepare,
        logger=_logger,
        name="goal engine",
    )
