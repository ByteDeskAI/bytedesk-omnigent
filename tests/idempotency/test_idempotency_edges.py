"""Edge tests for idempotency store engine and cache accessor."""

from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.idempotency import SqlAlchemyIdempotencyStore, get_idempotency_store


def test_engine_property_exposes_underlying_engine(tmp_path) -> None:
    store = SqlAlchemyIdempotencyStore(f"sqlite:///{tmp_path / 'idem.db'}")
    assert store.engine is not None


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_idempotency_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_idempotency_store.__globals__["_idempotency_store_cache"].clear()

    first = get_idempotency_store()
    second = get_idempotency_store()
    assert first is second
    assert isinstance(first, SqlAlchemyIdempotencyStore)
