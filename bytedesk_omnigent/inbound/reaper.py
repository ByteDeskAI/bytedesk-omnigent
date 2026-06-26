"""Dead-Letter reaper for the inbound pipeline (ADR-0155, BDP-2562).

**Dead Letter Channel** + bounded retry. A processor that fails in the request-path
fan-out records an ``inbound_event_results`` row with ``next_retry_at``; this
background loop (a sibling of the signal-bus reaper) re-dispatches due failures and,
at the attempt cap, dead-letters the result + its parent event. Guarded by a
**distinct** PG advisory lock (no-op on SQLite). The advisory-lock scaffold is the
shared ``advisory_locked_loop`` Template Method.
"""

from __future__ import annotations

import asyncio
import logging

from bytedesk_omnigent.inbound.pipeline import RETRY_BACKOFF_SECONDS, ProcessorOutcome
from bytedesk_omnigent.inbound.store import record_to_event
from bytedesk_omnigent.maintenance import advisory_locked_loop
from omnigent.db.utils import now_epoch

_logger = logging.getLogger(__name__)

# Stable 64-bit advisory-lock key for the inbound reaper ("inbnd_rt"), distinct
# from the signal-bus and memory-maintenance keys so the sweeps never contend.
_INBOUND_LOCK_KEY = 0x696E626E645F7274
_DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5


def run_inbound_retry_tick(
    store, processors, *, now: int | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> int:
    """Re-dispatch due failed results; dead-letter at the cap. Returns rows touched."""
    now = now_epoch() if now is None else now
    by_name = {p.name: p for p in processors}
    touched = 0
    for key, processor_name, attempts in store.due_retries(now=now):
        if attempts >= max_attempts:
            store.record_result(
                idempotency_key=key, processor=processor_name,
                status="dead_lettered", error="max attempts exceeded", now=now,
            )
            store.mark_status(key, "dead_lettered", now=now)
            touched += 1
            continue
        record = store.get(key)
        processor = by_name.get(processor_name)
        if record is None or processor is None:
            continue
        event = record_to_event(record)
        try:
            outcome = processor.handle(event)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            outcome = ProcessorOutcome(status="failed", detail=str(exc), retryable=True)
        next_retry = (
            now + RETRY_BACKOFF_SECONDS * (attempts + 1)
            if (outcome.status == "failed" and outcome.retryable)
            else None
        )
        store.record_result(
            idempotency_key=key, processor=processor_name, status=outcome.status,
            error=outcome.detail if outcome.status == "failed" else None,
            next_retry_at=next_retry, now=now,
        )
        touched += 1
    return touched


async def inbound_retry_reaper_loop(
    *, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS, lock_key: int = _INBOUND_LOCK_KEY
) -> None:
    """Background loop: every ``interval_seconds`` re-dispatch due inbound failures."""
    from bytedesk_omnigent.inbound.processors import all_processors
    from bytedesk_omnigent.inbound.store import get_inbound_event_store

    def _prepare():
        store = get_inbound_event_store()

        async def _work() -> None:
            touched = await asyncio.to_thread(run_inbound_retry_tick, store, all_processors())
            if touched:
                _logger.info("inbound retry reaper: touched=%d", touched)

        return store.engine, _work

    await advisory_locked_loop(
        interval_seconds=interval_seconds,
        lock_key=lock_key,
        prepare=_prepare,
        logger=_logger,
        name="inbound retry reaper",
    )
