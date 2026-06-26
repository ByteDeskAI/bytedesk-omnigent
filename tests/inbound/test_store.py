"""Tests for the inbound-event Message Store (ADR-0155, BDP-2559)."""
from __future__ import annotations

from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.store import SqlAlchemyInboundEventStore


def _store(tmp_path) -> SqlAlchemyInboundEventStore:
    return SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")


def _event(key="github:pr:repo#1:abc") -> InboundEvent:
    return InboundEvent(
        idempotency_key=key, source="github", type="pull_request.merged",
        occurred_at=100, received_at=100, raw_payload={"a": 1},
        normalized={"prNumber": 1}, headers={"x-github-event": "pull_request"},
        tenant_id="t1", event_id="d1")


def test_record_event_is_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    rec, inserted = store.record_event(_event(), now=100)
    assert inserted is True and rec.status == "received" and rec.normalized["prNumber"] == 1
    again, inserted2 = store.record_event(_event(), now=101)
    assert inserted2 is False  # duplicate key short-circuits
    assert again.idempotency_key == rec.idempotency_key


def test_mark_status_and_get(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(_event(), now=100)
    store.mark_status("github:pr:repo#1:abc", "fanned_out", now=102)
    assert store.get("github:pr:repo#1:abc").status == "fanned_out"
    assert store.get("missing") is None


def test_recent_returns_newest_first(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(_event("k1"), now=100)
    store.record_event(_event("k2"), now=200)
    recent = store.recent(limit=10)
    assert [r.idempotency_key for r in recent] == ["k2", "k1"]


def test_result_upsert_and_due_retries(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_event(_event(), now=100)
    key = "github:pr:repo#1:abc"
    store.record_result(idempotency_key=key, processor="goal-delivery", status="failed",
                        error="boom", next_retry_at=150, now=100)
    # not yet due
    assert store.due_retries(now=149) == []
    # due now
    due = store.due_retries(now=151)
    assert due == [(key, "goal-delivery", 1)]
    # upsert: success clears retry; attempts increments
    store.record_result(idempotency_key=key, processor="goal-delivery", status="ok", now=160)
    assert store.due_retries(now=999) == []
