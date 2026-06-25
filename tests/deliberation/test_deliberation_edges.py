"""Edge tests for deliberation store engine and cache accessor."""

from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.deliberation import SqlAlchemyDeliberationStore, get_deliberation_store


def test_engine_property_exposes_underlying_engine(tmp_path) -> None:
    store = SqlAlchemyDeliberationStore(f"sqlite:///{tmp_path / 'delib.db'}")
    assert store.engine is not None


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_deliberation_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_deliberation_store.__globals__["_deliberation_store_cache"].clear()

    first = get_deliberation_store()
    second = get_deliberation_store()
    assert first is second
    assert isinstance(first, SqlAlchemyDeliberationStore)
