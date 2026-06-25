"""Edge tests for peer store engine, cache accessor, and broadcast sends."""

from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore, get_peer_message_store


def test_engine_property_exposes_underlying_engine(tmp_path) -> None:
    store = SqlAlchemyPeerMessageStore(f"sqlite:///{tmp_path / 'peer.db'}")
    assert store.engine is not None


def test_send_broadcast_without_recipient(tmp_path) -> None:
    store = SqlAlchemyPeerMessageStore(f"sqlite:///{tmp_path / 'peer.db'}")
    msg = store.send(
        from_agent="maya", topic="team:eng", body="all hands", kind="broadcast", now=1
    )
    assert msg.to_agent is None
    assert store.topic_feed(topic="team:eng")[0].body == "all hands"


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_peer_message_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_peer_message_store.__globals__["_peer_store_cache"].clear()

    first = get_peer_message_store()
    second = get_peer_message_store()
    assert first is second
    assert isinstance(first, SqlAlchemyPeerMessageStore)
