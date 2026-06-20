"""Shared advisory-locked maintenance-loop factory (BDP-2355).

Three background loops — the signal-bus reaper (BDP-2248), the cron scheduler
(BDP-2250), and the accountability loop (BDP-2272) — shipped with a byte-identical
scaffold: ``while True`` → sleep → resolve store(s) → acquire a distinct PG
advisory lock (a no-op on SQLite) → run the blocking tick in a worker thread →
log → swallow a failed tick and continue → re-raise ``CancelledError`` for clean
shutdown. Only the interval, the lock key, the engine the lock binds to, and the
per-tick work differ.

:func:`advisory_locked_loop` is the **Template Method** for that scaffold: it owns
the invariant control flow (sleep / acquire-lock / resilient-except / cancel) and
calls back into a per-loop ``prepare`` hook for the two parts that vary — the
engine to lock on, and the async worker to run under the lock. Each loop keeps its
own distinct lock key so the sweeps never contend.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from omnigent.runtime.memory_maintenance import advisory_lock

# A loop's ``prepare`` resolves its store(s) and returns ``(engine, work)``: the
# engine the advisory lock binds to, and a zero-arg async worker run while the
# lock is held (typically an ``asyncio.to_thread`` of the blocking tick + a log).
PrepareTick = Callable[[], "tuple[object, Callable[[], Awaitable[None]]]"]


async def advisory_locked_loop(
    *,
    interval_seconds: int,
    lock_key: int,
    prepare: PrepareTick,
    logger: logging.Logger,
    name: str,
) -> None:
    """Run *prepare*'s tick every *interval_seconds* under *lock_key* (BDP-2355).

    The Template-Method scaffold shared by the three maintenance loops:

    - sleep ``interval_seconds`` between ticks;
    - call ``prepare()`` to resolve the store(s) → ``(engine, work)``;
    - acquire the ``lock_key`` advisory lock on ``engine`` (no-op on SQLite); if
      another instance holds it, skip this tick;
    - ``await work()`` while the lock is held (the worker runs the blocking tick
      in a thread and logs its own outcome);
    - a failed tick is logged via *logger* and the loop continues; a
      :class:`asyncio.CancelledError` propagates for clean shutdown.

    :param prepare: Per-loop hook returning ``(engine, work)`` (see
        :data:`PrepareTick`).
    :param name: Human label used in the failure log line (e.g. ``"cron scheduler"``).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            engine, work = prepare()
            with advisory_lock(engine, lock_key) as acquired:
                if not acquired:
                    continue
                await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s tick failed: %s", name, exc, exc_info=True)
