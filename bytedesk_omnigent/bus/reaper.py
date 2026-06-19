"""Periodic signal-bus reaper: expire stale pending waits (BDP-2248, ADR-0142).

Wired into the omnigent server's FastAPI lifespan as a background task, a direct
sibling of ``memory_maintenance_loop`` (FU1, ADR-0132). Each tick expires
``pending_waits`` whose ``expires_at`` has passed, guarded by a **distinct** PG
advisory lock so it runs on at most one server instance at a time (a no-op lock
on SQLite — local/dev/tests). The blocking DB work runs in a worker thread so
the event loop is never blocked. The advisory-lock helper is reused from
``memory_maintenance`` rather than reimplemented.
"""

from __future__ import annotations

import asyncio
import logging

from omnigent.runtime.memory_maintenance import advisory_lock

_logger = logging.getLogger(__name__)

# Stable 64-bit advisory-lock key for the signal-bus reaper ("sig_bus_").
# Distinct from the memory-maintenance key so the two sweeps never contend.
_SIGNAL_BUS_LOCK_KEY = 0x7369675F6275735F

_DEFAULT_INTERVAL_SECONDS = 60


def run_signal_bus_sweep_tick(bus, *, now: int | None = None) -> int:
    """Expire stale pending waits. Returns the number swept.

    :param bus: The :class:`~bytedesk_omnigent.bus.SqlAlchemySignalBus`.
    :param now: Current epoch seconds; defaults inside the call.
    """
    return bus.sweep_expired(now=now)


async def signal_bus_reaper_loop(
    *,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    lock_key: int = _SIGNAL_BUS_LOCK_KEY,
) -> None:
    """Background loop: every ``interval_seconds`` expire stale waits under the lock.

    Resilient — a failed tick is logged and the loop continues; cancellation
    propagates for clean shutdown.
    """
    from bytedesk_omnigent.runtime import get_signal_bus

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            bus = get_signal_bus()
            with advisory_lock(bus.engine, lock_key) as acquired:
                if not acquired:
                    continue
                swept = await asyncio.to_thread(run_signal_bus_sweep_tick, bus)
            if swept:
                _logger.info("signal bus reaper: expired=%d", swept)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("signal bus reaper tick failed: %s", exc, exc_info=True)
