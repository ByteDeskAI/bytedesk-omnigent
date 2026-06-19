"""ByteDesk durable-store runtime accessors (ADR-0143, BDP-2296).

Lazily-built, per-URI accessors for the ByteDesk durable substrate stores —
relocated out of the upstream-shared ``omnigent/runtime/__init__.py`` so that file
reverts to upstream. Each shares the canonical conversation store's database URI
(read from ``omnigent.runtime.get_conversation_store`` — extension→core, the
correct direction) and is cached per URI, mirroring the per-module store
accessors (``get_goal_store`` etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.runtime import get_conversation_store

if TYPE_CHECKING:
    from bytedesk_omnigent.bus import SqlAlchemySignalBus
    from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler
    from bytedesk_omnigent.tool_steps import SqlAlchemyToolStepStore


_signal_bus_cache: dict[str, SqlAlchemySignalBus] = {}


def get_signal_bus() -> SqlAlchemySignalBus:
    """Return the durable signal/await bus (BDP-2248, ADR-0142).

    Built lazily from the canonical conversation store's database URI and cached
    per URI. Backs the ``await_signal`` re-home + the inbound-ingress deliver path.
    """
    from bytedesk_omnigent.bus import SqlAlchemySignalBus

    location = get_conversation_store().storage_location
    bus = _signal_bus_cache.get(location)
    if bus is None:
        bus = SqlAlchemySignalBus(location)
        _signal_bus_cache[location] = bus
    return bus


_cron_scheduler_cache: dict[str, SqlAlchemyCronScheduler] = {}


def get_cron_scheduler() -> SqlAlchemyCronScheduler:
    """Return the native cron scheduler (BDP-2250, ADR-0142).

    Built lazily from the canonical conversation store's database URI and cached
    per URI. Drives the ``_lifespan`` cron loop — the org heartbeat.
    """
    from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler

    location = get_conversation_store().storage_location
    scheduler = _cron_scheduler_cache.get(location)
    if scheduler is None:
        scheduler = SqlAlchemyCronScheduler(location)
        _cron_scheduler_cache[location] = scheduler
    return scheduler


_tool_step_store_cache: dict[str, SqlAlchemyToolStepStore] = {}


def get_tool_step_store() -> SqlAlchemyToolStepStore:
    """Return the durable deterministic tool-step store (BDP-2252, ADR-0142).

    Built lazily from the canonical conversation store's database URI and cached
    per URI. Its ``resume_stale`` sweep runs at server boot to reclaim steps
    orphaned by a restart.
    """
    from bytedesk_omnigent.tool_steps import SqlAlchemyToolStepStore

    location = get_conversation_store().storage_location
    store = _tool_step_store_cache.get(location)
    if store is None:
        store = SqlAlchemyToolStepStore(location)
        _tool_step_store_cache[location] = store
    return store
