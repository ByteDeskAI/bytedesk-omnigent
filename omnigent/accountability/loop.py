"""The accountability tick + background loop (BDP-2272 C4, ADR-0142). See package docstring."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from omnigent.db.utils import now_epoch
from omnigent.runtime.memory_maintenance import advisory_lock

_logger = logging.getLogger(__name__)

# Stable 64-bit advisory-lock key ("acctblty") — distinct from the memory,
# signal-bus, and cron keys so the sweeps never contend.
_ACCOUNTABILITY_LOCK_KEY = 0x61636374626C7479

_DEFAULT_INTERVAL_SECONDS = 300
_DEFAULT_STALL_SECONDS = 3600


@dataclass(frozen=True)
class AccountabilityReport:
    """Outcome of one accountability tick."""

    rebalanced: int
    escalated: int


def run_accountability_tick(
    goals,
    peers,
    *,
    manager_agent_id: str | None = None,
    stall_seconds: int = _DEFAULT_STALL_SECONDS,
    now: int | None = None,
) -> AccountabilityReport:
    """Rebalance stalled owned goals + escalate blocked goals.

    ``goals`` / ``peers`` are the goal + peer stores (injectable so the tick is
    unit-provable). Rebalance reopens owned goals idle past ``stall_seconds`` and
    DMs the dropped owner. Escalation surfaces every ``blocked`` goal to
    ``manager_agent_id`` as a peer ``escalation`` message; when no manager is
    configured escalation is skipped (rebalance still runs).
    """
    now = now_epoch() if now is None else now

    reopened = goals.reopen_stalled(older_than_seconds=stall_seconds, now=now)
    for goal in reopened:
        if goal.owner_agent_id:
            peers.send(
                from_agent="accountability",
                to_agent=goal.owner_agent_id,
                topic="accountability:rebalance",
                kind="dm",
                body=(
                    f"Goal '{goal.title}' was reopened after stalling for "
                    f"{stall_seconds}s — it returns to the backlog for re-claim."
                ),
                now=now,
            )

    escalated = 0
    if manager_agent_id is not None:
        for goal in goals.list_goals(status="blocked"):
            peers.send(
                from_agent="accountability",
                to_agent=manager_agent_id,
                topic="accountability:escalation",
                kind="escalation",
                body=(
                    f"Goal '{goal.title}' (owner="
                    f"{goal.owner_agent_id or 'unassigned'}) is blocked and needs "
                    "a decision."
                ),
                now=now,
            )
            escalated += 1

    return AccountabilityReport(rebalanced=len(reopened), escalated=escalated)


async def accountability_loop(
    *,
    manager_agent_id: str | None = None,
    stall_seconds: int = _DEFAULT_STALL_SECONDS,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    lock_key: int = _ACCOUNTABILITY_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` run the accountability tick.

    Guarded by a distinct PG advisory lock (no-op on SQLite). Resilient — a failed
    tick is logged and the loop continues; cancellation propagates for clean
    shutdown. The blocking DB work runs in a worker thread.
    """
    from omnigent.goals import get_goal_store
    from omnigent.peer import get_peer_message_store

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            goals = get_goal_store()
            peers = get_peer_message_store()
            with advisory_lock(goals.engine, lock_key) as acquired:
                if not acquired:
                    continue
                report = await asyncio.to_thread(
                    run_accountability_tick,
                    goals,
                    peers,
                    manager_agent_id=manager_agent_id,
                    stall_seconds=stall_seconds,
                )
            if report.rebalanced or report.escalated:
                _logger.info(
                    "accountability: rebalanced=%d escalated=%d",
                    report.rebalanced,
                    report.escalated,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("accountability tick failed: %s", exc, exc_info=True)
