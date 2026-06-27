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

from bytedesk_omnigent.engine.dispatcher import ConversationSpawnPort, dispatch_goal
from bytedesk_omnigent.maintenance import advisory_locked_loop

_logger = logging.getLogger(__name__)

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
) -> int:
    """Dispatch every ready, immediate, owned ``assigned`` goal. Returns count spawned.

    A goal becomes workable once it is *claimed* (``assigned`` + an owner). The
    tick spawns its working session; ``dispatch_goal``'s unique session key makes a
    re-tick a no-op. Injectable stores so the tick is unit-provable without a live
    runner.

    BDP-2584 (additive): when ``sensor_registry`` is provided, each candidate is
    additionally gated through ``engine.resolver.resolve`` so a goal carrying a
    condition AST (or whose derived legacy condition is unmet) is held back until
    actionable. With no registry the behaviour is exactly Phase 1 — the
    ``activation_state == "ready"`` filter alone.
    """
    spawned = 0
    candidates = goal_store.list_goals(
        status="assigned",
        activation_state="ready",
        include_dependencies=sensor_registry is not None,
    )
    for goal in candidates:
        if goal.cadence_kind != "immediate" or not goal.owner_agent_id:
            continue
        if sensor_registry is not None:
            from bytedesk_omnigent.engine.resolver import resolve

            if not resolve(goal, registry=sensor_registry, goal_store=goal_store, now=now or 0)[
                "actionable"
            ]:
                continue
        result = dispatch_goal(
            goal,
            conversation_store=conversation_store,
            goal_store=goal_store,
            now=now,
        )
        if result.spawned:
            spawned += 1
    return spawned


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
    from bytedesk_omnigent.engine.sensors import build_default_registry
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    # BDP-2584: activate the condition resolver in production — each candidate is
    # gated through its sensor conditions (legacy no-AST goals resolve identically
    # to _activation_for, so this is behaviour-preserving for existing goals).
    sensor_registry = build_default_registry()

    def _prepare():
        goal_store = get_goal_store()
        conversation_store = get_conversation_store()

        async def _work() -> None:
            spawned = await asyncio.to_thread(
                run_goal_engine_tick,
                goal_store,
                conversation_store,
                sensor_registry=sensor_registry,
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
