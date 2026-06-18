"""Durable signal/await bus for omnigent core (BDP-2248, ADR-0142).

The single public surface for the durable inter-session signal/await bus that
replaces the ephemeral in-process inbox. Nothing in this package edits an
upstream file; the runtime accessor (``get_signal_bus``) and the lifespan reaper
wiring are the only additive seams elsewhere.
"""

from omnigent.bus.signal_bus import (
    DeliveryResult,
    DeliveryStatus,
    PendingWait,
    SqlAlchemySignalBus,
)

__all__ = [
    "DeliveryResult",
    "DeliveryStatus",
    "PendingWait",
    "SqlAlchemySignalBus",
]
