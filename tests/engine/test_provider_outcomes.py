"""Outcome booking for connected-app provider events."""
from __future__ import annotations

from bytedesk_omnigent.engine.providers.outcome import OutcomeProcessor
from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.inbound.event import InboundEvent


def test_outcome_processor_resolves_goal_from_subject_correlation(tmp_path, monkeypatch) -> None:
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    goal = store.create_goal(title="Close opportunity", now=100)
    store.record_goal_correlation(
        source="sales",
        subject_ref="opp-123",
        goal_id=goal.id,
        kind="opportunity",
        now=101,
    )

    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.engine.treasury.get_treasury", lambda: treasury)

    event = InboundEvent(
        idempotency_key="sales:opp-123",
        source="sales",
        type="outcome.booked",
        occurred_at=102,
        received_at=102,
        raw_payload={},
        normalized={
            "subjectRef": "opp-123",
            "realizedValueCents": 2500,
            "evidence": {"opportunityId": "opp-123"},
        },
    )

    result = OutcomeProcessor().handle(event)

    assert result.status == "ok"
    assert result.http_status == 202
    assert store.get_goal(goal_id=goal.id).realized_value_cents == 2500


def test_outcome_processor_parks_unresolved_subject_for_retry(tmp_path, monkeypatch) -> None:
    loc = f"sqlite:///{tmp_path / 'goals.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)

    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: store)
    monkeypatch.setattr("bytedesk_omnigent.engine.treasury.get_treasury", lambda: treasury)

    event = InboundEvent(
        idempotency_key="sales:missing",
        source="sales",
        type="outcome.booked",
        occurred_at=102,
        received_at=102,
        raw_payload={},
        normalized={"subjectRef": "missing", "realizedValueCents": 2500},
    )

    result = OutcomeProcessor().handle(event)

    assert result.status == "failed"
    assert result.http_status == 409
    assert result.retryable is True
    assert result.detail == "unresolved goal correlation"
