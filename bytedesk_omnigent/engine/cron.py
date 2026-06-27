"""Goal-aware cron dispatch (BDP-2583, ADR-0008 Adapter).

The cron scheduler fires a :class:`CronTrigger` and hands it to a dispatch
callable. ``goal_cron_dispatch`` wraps the existing dispatch so a fire whose
payload is a goal (``payload.kind == "goal"``) routes to
:func:`~bytedesk_omnigent.engine.dispatcher.dispatch_goal` — opening a session for
that fire — while every other trigger falls through to the original dispatch (the
fabric SQL outbox). The per-fire idempotency key is ``"{goal_id}:{next_fire_at}"``
so each scheduled occurrence gets its own session.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from bytedesk_omnigent.engine.dispatcher import ConversationSpawnPort, dispatch_goal
from bytedesk_omnigent.scheduler.scheduler import CronTrigger

_logger = logging.getLogger(__name__)

Dispatch = Callable[[CronTrigger], None]


def goal_cron_dispatch(
    *,
    conversation_store: ConversationSpawnPort,
    goal_store,
    fallback: Dispatch,
    sensor_registry=None,
) -> Dispatch:
    """Return a dispatch that routes goal triggers to the goal dispatcher.

    :param fallback: the dispatch for non-goal triggers (e.g. the fabric outbox).
    :param sensor_registry: when provided, the until_done heartbeat — before
        re-spawning a goal's fire, its ``success_condition`` is evaluated; a
        satisfied condition COMPLETES the goal (``done``) instead of spawning, and
        an already-``done`` goal never re-spawns. Without a registry the behaviour
        is unchanged (every fire re-spawns), so a plain recurring goal is unaffected
        (BDP-2596 feature 4).
    """

    def _dispatch(trigger: CronTrigger) -> None:
        payload = trigger.payload or {}
        if payload.get("kind") != "goal":
            fallback(trigger)
            return
        goal_id = payload.get("goal_id")
        goal = goal_store.get_goal(goal_id=goal_id) if goal_id else None
        if goal is None:
            _logger.warning("goal cron fire for missing goal_id=%s key=%s", goal_id, trigger.key)
            return
        # BDP-2596: a completed goal's trigger is a no-op — until_done goals stop
        # re-spawning once done (the trigger may still fire until it's deregistered).
        if str(goal.status) == "done":
            return
        # until_done heartbeat: if the success_condition has tripped, complete the
        # goal instead of re-spawning another working session this fire.
        if sensor_registry is not None and goal.success_condition is not None:
            from bytedesk_omnigent.engine.resolver import evaluate_success_condition

            if evaluate_success_condition(
                goal, registry=sensor_registry, goal_store=goal_store,
                now=trigger.next_fire_at,
            ):
                goal_store.advance_goal(
                    goal_id=goal.id, status="done", now=trigger.next_fire_at
                )
                _logger.info("until_done goal completed on heartbeat: goal=%s", goal.id)
                return
        period_key = f"{goal.id}:{trigger.next_fire_at}"
        result = dispatch_goal(
            goal,
            conversation_store=conversation_store,
            goal_store=goal_store,
            period_key=period_key,
        )
        if result.spawned:
            _logger.info(
                "goal cron dispatch: goal=%s session=%s period=%s",
                goal.id,
                result.session_id,
                period_key,
            )

    return _dispatch


def build_goal_cron_dispatch(fallback: Dispatch) -> Dispatch:
    """Production wiring: resolve the canonical stores, wrap *fallback* (BDP-2583).

    BDP-2596: the dispatch is built with the default sensor registry so until_done
    goals complete on their heartbeat when their success_condition trips.
    """
    from bytedesk_omnigent.engine.sensors import build_default_registry
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    return goal_cron_dispatch(
        conversation_store=get_conversation_store(),
        goal_store=get_goal_store(),
        fallback=fallback,
        sensor_registry=build_default_registry(),
    )
