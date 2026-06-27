"""The OutcomeSource sink: a provider-pushed outcome event books realized value.

The ``OutcomeSource`` role is a *sink*, not a polled object — a connected app
PUSHEs realized value into the engine. It is implemented as the canonical inbound
ingress (Phase 4) feeding an :class:`OutcomeProcessor` (an
:class:`~bytedesk_omnigent.inbound.pipeline.InboundProcessor` Observer) that routes
``outcome.booked`` events to :func:`~bytedesk_omnigent.engine.treasury.book_outcome`
— the single writer of realized value (the flywheel's external input).

The pipeline gives idempotency for free: a redelivered ``outcome.booked`` is
short-circuited as a duplicate before this processor ever runs, so the ledger
cannot double-count.
"""
from __future__ import annotations

from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.pipeline import ProcessorOutcome

OUTCOME_EVENT_TYPE = "outcome.booked"


class OutcomeProcessor:
    """Book a provider-pushed realized outcome into the treasury ledger."""

    name = "outcome-source"

    def interested(self, event: InboundEvent) -> bool:
        return event.type == OUTCOME_EVENT_TYPE

    def handle(self, event: InboundEvent) -> ProcessorOutcome:
        from bytedesk_omnigent.engine.treasury import get_treasury
        from bytedesk_omnigent.goals import get_goal_store

        n = event.normalized
        goal_id = n.get("goalId")
        cents = n.get("realizedValueCents")
        if not goal_id or cents is None:
            return ProcessorOutcome(
                status="skipped", http_status=400, detail="missing goalId/realizedValueCents"
            )
        outcome = get_treasury().book_outcome(
            goal_store=get_goal_store(),
            goal_id=str(goal_id),
            realized_value_cents=int(cents),
            source=event.source,
            evidence=n.get("evidence"),
        )
        if outcome is None:
            return ProcessorOutcome(status="skipped", http_status=404, detail="unknown goal")
        return ProcessorOutcome(status="ok", http_status=202, detail=outcome.id)


__all__ = ["OUTCOME_EVENT_TYPE", "OutcomeProcessor"]
