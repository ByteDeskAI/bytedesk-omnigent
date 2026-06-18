"""Native cron scheduler background loop (BDP-2250, ADR-0142).

Wired into the omnigent server's FastAPI lifespan as a background task — a direct
sibling of ``memory_maintenance_loop`` (ADR-0132) and ``signal_bus_reaper_loop``
(BDP-2248). Each tick finds due triggers, claims each exactly-once under a
**distinct** PG advisory lock (no-op on SQLite), and dispatches the claimed ones.

The **dispatch** is an injectable Strategy seam (ADR-0008). The default logs only
— the real session-opening dispatch (open a root session + post the trigger
payload) is wired in the scheduler re-home follow-up, mirroring how the signal
bus shipped its store first and deferred the runner wake-hook.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from omnigent.runtime.memory_maintenance import advisory_lock
from omnigent.scheduler.scheduler import CronTrigger, run_cron_scheduler_tick

_logger = logging.getLogger(__name__)

# Stable 64-bit advisory-lock key for the cron scheduler ("cron_sch"). Distinct
# from the memory-maintenance and signal-bus keys so the sweeps never contend.
_CRON_LOCK_KEY = 0x63726F6E5F736368

_DEFAULT_INTERVAL_SECONDS = 30


def _log_only_dispatch(trigger: CronTrigger) -> None:
    """Default dispatch: log the fire without running an agent.

    The real dispatch (open/resume the agent's session + post ``trigger.payload``
    as a message) is wired in the scheduler re-home follow-up; until then the
    scheduler is the proven durable *clock*, exactly as the signal bus shipped
    its durable store before the runner wake-hook.
    """
    _logger.info(
        "cron fire (no dispatch wired yet): agent=%s key=%s",
        trigger.agent_id,
        trigger.key,
    )


async def cron_scheduler_loop(
    *,
    dispatch: Callable[[CronTrigger], None] | None = None,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    lock_key: int = _CRON_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` fire due triggers under the lock.

    Resilient — a failed tick is logged and the loop continues; cancellation
    propagates for clean shutdown. The blocking DB work runs in a worker thread.
    """
    from omnigent.runtime import get_cron_scheduler

    if dispatch is None:
        dispatch = _log_only_dispatch

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            scheduler = get_cron_scheduler()
            with advisory_lock(scheduler.engine, lock_key) as acquired:
                if not acquired:
                    continue
                fired = await asyncio.to_thread(
                    run_cron_scheduler_tick, scheduler, dispatch
                )
            if fired:
                _logger.info("cron scheduler: fired=%d", fired)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("cron scheduler tick failed: %s", exc, exc_info=True)
