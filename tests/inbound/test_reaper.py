"""Tests for the inbound Dead-Letter reaper (ADR-0155, BDP-2562)."""
from __future__ import annotations

from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.pipeline import ProcessorOutcome
from bytedesk_omnigent.inbound.reaper import run_inbound_retry_tick
from bytedesk_omnigent.inbound.store import SqlAlchemyInboundEventStore


def _store(tmp_path) -> SqlAlchemyInboundEventStore:
    return SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")


def _seed_failed(store, key="k", processor="p", attempts_field=1, next_retry_at=100):
    ev = InboundEvent(idempotency_key=key, source="github", type="pull_request.merged",
                      occurred_at=1, received_at=1, raw_payload={"a": 1}, normalized={"x": 1})
    store.record_event(ev, now=1)
    store.record_result(idempotency_key=key, processor=processor, status="failed",
                        error="boom", next_retry_at=next_retry_at, now=1)


class _Proc:
    def __init__(self, name, *, succeed):
        self.name = name
        self._succeed = succeed
        self.calls = 0

    def interested(self, event):
        return True

    def handle(self, event):
        self.calls += 1
        if self._succeed:
            return ProcessorOutcome(status="ok")
        return ProcessorOutcome(status="failed", detail="still broken", retryable=True)


def test_due_failure_redispatched_and_succeeds(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_failed(store, processor="p", next_retry_at=100)
    proc = _Proc("p", succeed=True)
    touched = run_inbound_retry_tick(store, [proc], now=150)  # past next_retry_at
    assert touched == 1 and proc.calls == 1
    assert store.due_retries(now=99999) == []  # no longer failed


def test_not_yet_due_is_skipped(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_failed(store, processor="p", next_retry_at=200)
    proc = _Proc("p", succeed=True)
    assert run_inbound_retry_tick(store, [proc], now=150) == 0  # before next_retry_at
    assert proc.calls == 0


def test_dead_letters_at_attempt_cap(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_failed(store, key="k", processor="p", next_retry_at=100)
    proc = _Proc("p", succeed=False)
    # drive attempts up to the cap (5); each retry that still fails reschedules
    now = 150
    for _ in range(10):
        run_inbound_retry_tick(store, [proc], now=now, max_attempts=5)
        now += 10_000  # jump past each next_retry_at
    # parent event ends dead_lettered, retries drained
    assert store.get("k").status == "dead_lettered"
    assert store.due_retries(now=999_999) == []
