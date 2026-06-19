"""Tests for the durable peer-message store (BDP-2270 C2, ADR-0142)."""
from __future__ import annotations

import time

from bytedesk_omnigent.peer import SqlAlchemyPeerMessageStore


def _store(tmp_path) -> SqlAlchemyPeerMessageStore:
    return SqlAlchemyPeerMessageStore(f"sqlite:///{tmp_path / 'peer.db'}")


def test_dm_inbox_unread_then_mark_read(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())

    store.send(
        from_agent="caleb", to_agent="priya", topic="dm:caleb:priya",
        body="can you review the dotnet PR?", kind="dm", now=now,
    )
    store.send(
        from_agent="caleb", to_agent="priya", topic="dm:caleb:priya",
        body="bump — still blocked", kind="dm", now=now + 1,
    )

    unread = store.inbox(to_agent="priya", unread_only=True)
    assert [m.body for m in unread] == [
        "can you review the dotnet PR?",
        "bump — still blocked",
    ]

    # Draining with mark_read clears them from the unread inbox.
    store.inbox(to_agent="priya", unread_only=True, mark_read=True, now=now + 2)
    assert store.inbox(to_agent="priya", unread_only=True) == []
    # But they remain in the (read-inclusive) view.
    assert len(store.inbox(to_agent="priya", unread_only=False)) == 2


def test_topic_feed_collects_broadcasts(tmp_path) -> None:
    store = _store(tmp_path)
    now = int(time.time())
    store.send(from_agent="maya", topic="team:eng", body="standup in 5", kind="broadcast", now=now)
    store.send(from_agent="elias", topic="team:eng", body="deploy is green", kind="broadcast", now=now + 1)
    store.send(from_agent="maya", topic="team:sales", body="other topic", kind="broadcast", now=now)

    feed = store.topic_feed(topic="team:eng")
    assert [m.body for m in feed] == ["standup in 5", "deploy is green"]
