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
) -> int:
    """Dispatch every ready, immediate, owned ``assigned`` goal. Returns count spawned.

    A goal becomes workable once it is *claimed* (``assigned`` + an owner). The
    tick spawns its working session; ``dispatch_goal``'s unique session key makes a
    re-tick a no-op. Injectable stores so the tick is unit-provable without a live
    runner.
    """
    spawned = 0
    candidates = goal_store.list_goals(status="assigned", activation_state="ready")
    for goal in candidates:
        if goal.cadence_kind != "immediate" or not goal.owner_agent_id:
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
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    def _prepare():
        goal_store = get_goal_store()
        conversation_store = get_conversation_store()

        async def _work() -> None:
            spawned = await asyncio.to_thread(
                run_goal_engine_tick, goal_store, conversation_store
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
