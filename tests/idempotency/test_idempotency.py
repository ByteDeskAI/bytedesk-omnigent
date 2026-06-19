"""Tests for the durable idempotency-key store: at-most-once claim
(BDP-2251, ADR-0142, aligned ADR-0009/0077)."""
from __future__ import annotations

from bytedesk_omnigent.idempotency import SqlAlchemyIdempotencyStore


def _store(tmp_path) -> SqlAlchemyIdempotencyStore:
    return SqlAlchemyIdempotencyStore(f"sqlite:///{tmp_path / 'idem.db'}")


def test_claim_is_atomic_first_caller_wins(tmp_path) -> None:
    store = _store(tmp_path)

    # First claim of (scope, key) wins; a duplicate delivery loses.
    assert store.claim(scope="event-trigger", key="msg-1") is True
    assert store.claim(scope="event-trigger", key="msg-1") is False
    assert store.is_claimed(scope="event-trigger", key="msg-1") is True

    # A different key, or the same key under a different scope, is independent.
    assert store.claim(scope="event-trigger", key="msg-2") is True
    assert store.claim(scope="support-ticket", key="msg-1") is True
    assert store.is_claimed(scope="event-trigger", key="msg-3") is False


def test_mark_dead_lettered_after_claim(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.claim(scope="release", key="v1.2.3") is True
    # Work failed past redelivery — mark it; the key stays claimed (no re-run).
    store.mark_dead_lettered(scope="release", key="v1.2.3", result={"error": "timeout"})
    assert store.claim(scope="release", key="v1.2.3") is False
