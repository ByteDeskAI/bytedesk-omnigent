"""The accountability tick + background loop (BDP-2272 C4, ADR-0142). See package docstring."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bytedesk_omnigent.maintenance import advisory_locked_loop
from omnigent.db.utils import now_epoch

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
    # BDP-2596: cents of budget redeployed from stalled scopes to high-ROI scopes.
    reallocated_cents: int = 0


def run_accountability_tick(
    goals,
    peers,
    *,
    manager_agent_id: str | None = None,
    stall_seconds: int = _DEFAULT_STALL_SECONDS,
    treasury=None,
    now: int | None = None,
) -> AccountabilityReport:
    """Rebalance stalled owned goals + escalate blocked goals.

    ``goals`` / ``peers`` are the goal + peer stores (injectable so the tick is
    unit-provable). Rebalance reopens owned goals idle past ``stall_seconds`` and
    DMs the dropped owner. Escalation surfaces every ``blocked`` goal to
    ``manager_agent_id`` as a peer ``escalation`` message; when no manager is
    configured escalation is skipped (rebalance still runs).

    BDP-2596: when ``treasury`` is provided, the tick ALSO reallocates budget — it
    harvests idle headroom from the scopes of goals reopened this tick (stalled =
    holding budget it isn't converting) and redeploys it to the highest-ROI active
    scope, recorded as treasury decisions. Without a treasury this is unchanged
    (reopen + escalate only).
    """
    now = now_epoch() if now is None else now

    reopened = goals.reopen_stalled(older_than_seconds=stall_seconds, now=now)
    reallocated_cents = 0
    if treasury is not None:
        from bytedesk_omnigent.engine.rebalance import rebalance_budget

        reallocated_cents = rebalance_budget(treasury, reopened, now=now)
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
        # escalate_blocked claims each blocked goal ONCE (escalated_at dedup), so a
        # re-tick returns nothing — no escalation spam (BDP-2283).
        for goal in goals.escalate_blocked(now=now):
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

    return AccountabilityReport(
        rebalanced=len(reopened),
        escalated=escalated,
        reallocated_cents=reallocated_cents,
    )


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
    from bytedesk_omnigent.engine.treasury import get_treasury
    from bytedesk_omnigent.goals import get_goal_store
    from bytedesk_omnigent.peer import get_peer_message_store

    def _prepare():
        goals = get_goal_store()
        peers = get_peer_message_store()
        treasury = get_treasury()

        async def _work() -> None:
            report = await asyncio.to_thread(
                run_accountability_tick,
                goals,
                peers,
                manager_agent_id=manager_agent_id,
                stall_seconds=stall_seconds,
                treasury=treasury,
            )
            if report.rebalanced or report.escalated or report.reallocated_cents:
                _logger.info(
                    "accountability: rebalanced=%d escalated=%d reallocated_cents=%d",
                    report.rebalanced,
                    report.escalated,
                    report.reallocated_cents,
                )

        return goals.engine, _work

    await advisory_locked_loop(
        interval_seconds=interval_seconds,
        lock_key=lock_key,
        prepare=_prepare,
        logger=_logger,
        name="accountability",
    )
