"""The inbound-event pipeline core (ADR-0155, BDP-2560).

**Pipes and Filters** (EIP): one ``ingest()`` chain every Channel-Adapter route feeds
— translate → wire-tap (log + realtime emit) → **Idempotent Receiver** claim →
**Content-Based Router** + **Observer** fan-out to interested processors. Pure +
injectable (store, processors, emit) so it is unit-proven without FastAPI, exactly
like ``process_inbound``. ``IngestResult`` carries the HTTP status the thin route returns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.store import InboundEventRecord, SqlAlchemyInboundEventStore
from bytedesk_omnigent.inbound.translators import resolve_translator
from omnigent.db.utils import now_epoch

# Bounded retry backoff (secs) for a failed processor before the reaper re-dispatches.
RETRY_BACKOFF_SECONDS = 60


@dataclass(frozen=True)
class ProcessorOutcome:
    """What one processor did with one event. ``status``: ok | skipped | failed."""

    status: str
    http_status: int = 202
    detail: str | None = None
    retryable: bool = False


@runtime_checkable
class InboundProcessor(Protocol):
    """A registered consumer of canonical events (Observer + Message Filter)."""

    name: str

    def interested(self, event: InboundEvent) -> bool: ...

    def handle(self, event: InboundEvent) -> ProcessorOutcome: ...


@dataclass(frozen=True)
class IngestResult:
    """Outcome of ``ingest`` — carries the HTTP status the route returns."""

    status: str
    http_status: int
    idempotency_key: str | None = None
    event_type: str | None = None
    duplicate: bool = False
    detail: str | None = None
    outcomes: dict[str, ProcessorOutcome] = field(default_factory=dict)


def ingest(
    *,
    channel: str,
    source: str,
    raw_payload: dict[str, Any],
    headers: Mapping[str, str],
    store: SqlAlchemyInboundEventStore,
    processors: Sequence[InboundProcessor],
    emit: Callable[[InboundEventRecord, bool], None] | None = None,
    now: int | None = None,
) -> IngestResult:
    """Run one inbound event through the pipeline. Steps mirror the ADR-0155 flow."""
    now = now_epoch() if now is None else now

    # 1. Message Translator — raw → canonical (None = non-actionable, ack "ignored").
    translator = resolve_translator(channel)
    if translator is None:
        return IngestResult(status="unknown_channel", http_status=404, detail=channel)
    event = translator.translate(source=source, raw_payload=raw_payload, headers=headers, now=now)
    if event is None:
        return IngestResult(status="ignored", http_status=202, detail="non-actionable payload")

    # 2. Wire Tap + 3. Idempotent Receiver — one row; INSERT claims, replay short-circuits.
    record, inserted = store.record_event(event, now=now)
    if emit is not None:
        emit(record, inserted)  # tee even duplicates so they're observable
    if not inserted:
        return IngestResult(
            status="duplicate", http_status=200, idempotency_key=event.idempotency_key,
            event_type=event.type, duplicate=True, detail="already seen",
        )

    # 4. Content-Based Router + 5. Observer fan-out.
    interested = [p for p in processors if p.interested(event)]
    outcomes: dict[str, ProcessorOutcome] = {}
    for processor in interested:
        try:
            outcome = processor.handle(event)
        except Exception as exc:  # noqa: BLE001 - one bad processor must not block siblings
            outcome = ProcessorOutcome(
                status="failed", http_status=500, detail=str(exc), retryable=True
            )
        outcomes[processor.name] = outcome
        store.record_result(
            idempotency_key=event.idempotency_key,
            processor=processor.name,
            status=outcome.status,
            error=outcome.detail if outcome.status == "failed" else None,
            next_retry_at=(now + RETRY_BACKOFF_SECONDS)
            if (outcome.status == "failed" and outcome.retryable)
            else None,
            now=now,
        )

    http_status, status = _aggregate(interested, outcomes)
    store.mark_status(event.idempotency_key, "fanned_out", now=now)
    return IngestResult(
        status=status, http_status=http_status, idempotency_key=event.idempotency_key,
        event_type=event.type, outcomes=outcomes,
    )


def _aggregate(
    interested: Sequence[InboundProcessor], outcomes: Mapping[str, ProcessorOutcome]
) -> tuple[int, str]:
    """Aggregate per-processor outcomes into one HTTP status for the route.

    No consumer → 404 (BDP-1419: never silently 2xx a no-match). Any failure → 500
    (sender retries; the reaper also retries). All skipped → 404 (no-match parity
    with the legacy goal-delivery 404). Otherwise → the matched processor's status.
    """
    if not interested:
        return 404, "no_consumer"
    if any(o.status == "failed" for o in outcomes.values()):
        return 500, "processing_failed"
    if all(o.status == "skipped" for o in outcomes.values()):
        # mirror legacy no-match: a single processor saw it but matched nothing
        only = next(iter(outcomes.values()))
        return only.http_status if only.http_status != 202 else 404, "no_match"
    # at least one ok — prefer the first ok processor's http status
    ok = next((o for o in outcomes.values() if o.status == "ok"), None)
    return (ok.http_status if ok else 202), "projected"
