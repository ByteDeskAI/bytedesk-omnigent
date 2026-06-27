"""Canonical provider ingress + outcome sink (Phase 4, BDP-2586).

Proves: (1) the canonical translator builds an InboundEvent the pipeline fans out,
(2) ADR-0155 ``pipeline.ingest`` now has a live caller, (3) an ``outcome.booked``
event books realized value via ``treasury.book_outcome`` (asserts the ledger row).
No network/LLM.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.providers.ingress import CHANNEL_PROVIDER, CanonicalTranslator
from bytedesk_omnigent.engine.providers.outcome import OUTCOME_EVENT_TYPE, OutcomeProcessor
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.inbound.pipeline import ProcessorOutcome, ingest
from bytedesk_omnigent.inbound.store import SqlAlchemyInboundEventStore
from bytedesk_omnigent.inbound.translators import register_translator


def _inbound_store(tmp_path) -> SqlAlchemyInboundEventStore:
    return SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")


# -- canonical translator -----------------------------------------------------
def test_canonical_translator_passthrough() -> None:
    t = CanonicalTranslator()
    event = t.translate(
        source="bytedesk",
        raw_payload={"type": "signal.x", "normalized": {"a": 1}, "occurred_at": 7},
        headers={},
        now=99,
    )
    assert event is not None
    assert event.type == "signal.x"
    assert event.source == "bytedesk"
    assert event.normalized == {"a": 1}
    assert event.occurred_at == 7
    assert event.idempotency_key.startswith("provider:bytedesk:signal.x:")


def test_canonical_translator_ignores_typeless() -> None:
    assert CanonicalTranslator().translate(source="x", raw_payload={}, headers={}, now=1) is None


# -- live caller of pipeline.ingest -------------------------------------------
class _SpyProcessor:
    name = "spy"

    def __init__(self) -> None:
        self.events: list = []

    def interested(self, event) -> bool:
        return True

    def handle(self, event) -> ProcessorOutcome:
        self.events.append(event)
        return ProcessorOutcome(status="ok", http_status=202)


def test_pipeline_ingest_has_live_caller_via_canonical_channel(tmp_path) -> None:
    register_translator(CHANNEL_PROVIDER, CanonicalTranslator)
    store = _inbound_store(tmp_path)
    spy = _SpyProcessor()

    result = ingest(
        channel=CHANNEL_PROVIDER,
        source="bytedesk",
        raw_payload={"type": "custom.signal", "idempotency_key": "k-1"},
        headers={},
        store=store,
        processors=[spy],
        now=100,
    )

    assert result.status == "projected"
    assert result.idempotency_key == "k-1"
    assert len(spy.events) == 1 and spy.events[0].type == "custom.signal"
    # wire-tap row persisted -> the pipeline really ran
    assert store.get("k-1") is not None


# -- outcome sink books realized value via treasury ---------------------------
def test_outcome_processor_books_via_treasury(tmp_path, monkeypatch) -> None:
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    goal_store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    goal = goal_store.create_goal(title="revenue goal")

    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goal_store)
    monkeypatch.setattr("bytedesk_omnigent.engine.treasury.get_treasury", lambda: treasury)

    from bytedesk_omnigent.inbound.event import InboundEvent

    event = InboundEvent(
        idempotency_key="o-1",
        source="bytedesk",
        type=OUTCOME_EVENT_TYPE,
        occurred_at=1,
        received_at=1,
        raw_payload={},
        normalized={"goalId": goal.id, "realizedValueCents": 5000, "evidence": {"inv": "INV-1"}},
    )
    out = OutcomeProcessor().handle(event)

    assert out.status == "ok"
    ledger = treasury.outcomes(goal_id=goal.id)
    assert len(ledger) == 1
    assert ledger[0].realized_value_cents == 5000
    assert ledger[0].source == "bytedesk"
    assert ledger[0].evidence == {"inv": "INV-1"}
    # the goal's realized value was bumped (the flywheel input)
    bumped = goal_store.get_goal(goal_id=goal.id)
    assert bumped.realized_value_cents == 5000


def test_outcome_processor_skips_missing_fields() -> None:
    from bytedesk_omnigent.inbound.event import InboundEvent

    event = InboundEvent(
        idempotency_key="o-2", source="x", type=OUTCOME_EVENT_TYPE,
        occurred_at=1, received_at=1, raw_payload={}, normalized={},
    )
    out = OutcomeProcessor().handle(event)
    assert out.status == "skipped" and out.http_status == 400


def test_outcome_processor_only_interested_in_outcome_type() -> None:
    from bytedesk_omnigent.inbound.event import InboundEvent

    p = OutcomeProcessor()
    base = dict(idempotency_key="k", source="x", occurred_at=1, received_at=1,
                raw_payload={}, normalized={})
    assert p.interested(InboundEvent(type=OUTCOME_EVENT_TYPE, **base)) is True
    assert p.interested(InboundEvent(type="pull_request.merged", **base)) is False
