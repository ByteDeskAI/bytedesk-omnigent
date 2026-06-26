"""Tests for the inbound pipeline core (ADR-0155, BDP-2560)."""
from __future__ import annotations

from bytedesk_omnigent.inbound.pipeline import ProcessorOutcome, ingest
from bytedesk_omnigent.inbound.store import SqlAlchemyInboundEventStore
from bytedesk_omnigent.inbound.translators import CHANNEL_GOAL_DELIVERY

_GH_MERGED = {
    "action": "closed",
    "pull_request": {"number": 987, "merged": True, "head": {"ref": "feature/x"},
                     "base": {"ref": "develop"}, "merge_commit_sha": "deadbeef"},
    "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
}
_HDRS = {"x-github-delivery": "guid-1"}


def _store(tmp_path) -> SqlAlchemyInboundEventStore:
    return SqlAlchemyInboundEventStore(f"sqlite:///{tmp_path / 'inbound.db'}")


class _FakeProcessor:
    def __init__(self, name, *, types, outcome=None, raises=False):
        self.name = name
        self._types = types
        self._outcome = outcome or ProcessorOutcome(status="ok", http_status=202)
        self._raises = raises
        self.handled = 0

    def interested(self, event):
        return event.type in self._types

    def handle(self, event):
        self.handled += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._outcome


def _ingest(store, processors, payload=_GH_MERGED, emit=None, now=100):
    return ingest(channel=CHANNEL_GOAL_DELIVERY, source="github", raw_payload=payload,
                  headers=_HDRS, store=store, processors=processors, emit=emit, now=now)


def test_ingest_translates_logs_and_fans_out(tmp_path) -> None:
    store = _store(tmp_path)
    proc = _FakeProcessor("goal-delivery", types={"pull_request.merged"})
    r = _ingest(store, [proc])
    assert r.status == "projected" and r.http_status == 202
    assert proc.handled == 1
    rec = store.get(r.idempotency_key)
    assert rec is not None and rec.status == "fanned_out"


def test_ingest_dedupes_replay(tmp_path) -> None:
    store = _store(tmp_path)
    proc = _FakeProcessor("goal-delivery", types={"pull_request.merged"})
    first = _ingest(store, [proc], now=100)
    second = _ingest(store, [proc], now=101)
    assert first.duplicate is False and second.duplicate is True and second.http_status == 200
    assert proc.handled == 1  # fan-out fires exactly once


def test_ingest_ignores_non_actionable(tmp_path) -> None:
    store = _store(tmp_path)
    r = _ingest(store, [], payload={"action": "opened", "pull_request": {"number": 1}})
    assert r.status == "ignored" and r.http_status == 202
    assert store.recent() == []  # nothing logged


def test_ingest_no_consumer_is_404(tmp_path) -> None:
    store = _store(tmp_path)
    proc = _FakeProcessor("other", types={"email.received"})  # not interested
    r = _ingest(store, [proc])
    assert r.status == "no_consumer" and r.http_status == 404
    assert store.get(r.idempotency_key) is not None  # still logged (observable)


def test_ingest_skipped_is_no_match_404(tmp_path) -> None:
    store = _store(tmp_path)
    proc = _FakeProcessor("goal-delivery", types={"pull_request.merged"},
                          outcome=ProcessorOutcome(status="skipped", http_status=404))
    r = _ingest(store, [proc])
    assert r.status == "no_match" and r.http_status == 404


def test_failed_processor_records_retry_and_still_logs(tmp_path) -> None:
    store = _store(tmp_path)
    proc = _FakeProcessor("goal-delivery", types={"pull_request.merged"}, raises=True)
    r = _ingest(store, [proc], now=100)
    assert r.status == "processing_failed" and r.http_status == 500
    # wire-tap row exists even though the processor threw
    assert store.get(r.idempotency_key) is not None
    # a retry is scheduled
    assert store.due_retries(now=1000) == [(r.idempotency_key, "goal-delivery", 1)]
