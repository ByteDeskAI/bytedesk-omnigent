"""Native cron scheduler for omnigent core (BDP-2250, ADR-0142).

Durable, single-writer scheduled triggers that fire an agent on a cadence — the
heartbeat that turns the org from request-driven into self-waking. Mirrors the
durable signal bus (``omnigent/bus/``): a SQLAlchemy store over a new table, a
``_lifespan`` loop guarded by a distinct advisory lock, and a runtime accessor.
"""

from omnigent.scheduler.loop import cron_scheduler_loop
from omnigent.scheduler.scheduler import (
    CronTrigger,
    SqlAlchemyCronScheduler,
    compute_next_fire,
    run_cron_scheduler_tick,
)

__all__ = [
    "CronTrigger",
    "SqlAlchemyCronScheduler",
    "compute_next_fire",
    "cron_scheduler_loop",
    "run_cron_scheduler_tick",
]
