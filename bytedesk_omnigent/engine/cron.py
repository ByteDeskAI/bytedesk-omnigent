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
) -> Dispatch:
    """Return a dispatch that routes goal triggers to the goal dispatcher.

    :param fallback: the dispatch for non-goal triggers (e.g. the fabric outbox).
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
    """Production wiring: resolve the canonical stores, wrap *fallback* (BDP-2583)."""
    from bytedesk_omnigent.goals import get_goal_store
    from omnigent.runtime import get_conversation_store

    return goal_cron_dispatch(
        conversation_store=get_conversation_store(),
        goal_store=get_goal_store(),
        fallback=fallback,
    )
