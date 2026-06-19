"""Durable peer-message store (BDP-2270 C2, ADR-0142).

The lateral social fabric: an agent can ask a peer, escalate sideways, or push
up — not just answer down an ``allowed_subagents`` tree. ``SqlAlchemyPeerMessageStore``
is the durable data plane (per-recipient inbox + per-topic feed); the
``sys_peer_message`` agent tool and the always-on wake (via the cron/signal
substrate) are the integration follow-up. Shares the conversation store's engine,
like the other omnigent-native stores.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from bytedesk_omnigent.db_models import SqlPeerMessage
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class PeerMessage:
    """A row of ``peer_messages``."""

    id: str
    seq: int
    from_agent: str
    to_agent: str | None
    topic: str
    kind: str
    body: str
    created_at: int
    read_at: int | None


def _to_peer(row: SqlPeerMessage) -> PeerMessage:
    return PeerMessage(
        id=row.id,
        seq=row.seq,
        from_agent=row.from_agent,
        to_agent=row.to_agent,
        topic=row.topic,
        kind=row.kind,
        body=row.body,
        created_at=row.created_at,
        read_at=row.read_at,
    )


class SqlAlchemyPeerMessageStore:
    """Durable lateral peer-message store (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def send(
        self,
        *,
        from_agent: str,
        topic: str,
        body: str,
        to_agent: str | None = None,
        kind: str = "dm",
        now: int | None = None,
    ) -> PeerMessage:
        """Send a peer message. ``to_agent=None`` is a topic broadcast."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            next_seq = (
                session.execute(
                    select(func.coalesce(func.max(SqlPeerMessage.seq), 0))
                ).scalar_one()
                + 1
            )
            row = SqlPeerMessage(
                id=f"pm_{uuid.uuid4().hex}",
                seq=next_seq,
                from_agent=from_agent,
                to_agent=to_agent,
                topic=topic,
                kind=kind,
                body=body,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_peer(row)

    def inbox(
        self,
        *,
        to_agent: str,
        unread_only: bool = True,
        mark_read: bool = False,
        now: int | None = None,
    ) -> list[PeerMessage]:
        """Return ``to_agent``'s messages (FIFO by ``seq``)."""
        now = now_epoch() if now is None else now
        stmt = select(SqlPeerMessage).where(SqlPeerMessage.to_agent == to_agent)
        if unread_only:
            stmt = stmt.where(SqlPeerMessage.read_at.is_(None))
        stmt = stmt.order_by(SqlPeerMessage.seq)
        with self._write_session() as session:
            rows = session.execute(stmt).scalars().all()
            out = [_to_peer(r) for r in rows]
            if mark_read:
                for r in rows:
                    r.read_at = now
            return out

    def topic_feed(self, *, topic: str, limit: int = 50) -> list[PeerMessage]:
        """Return the most-recent messages on a topic (FIFO by ``seq``)."""
        stmt = (
            select(SqlPeerMessage)
            .where(SqlPeerMessage.topic == topic)
            .order_by(SqlPeerMessage.seq)
            .limit(limit)
        )
        with self._session() as session:
            return [_to_peer(r) for r in session.execute(stmt).scalars().all()]


_peer_store_cache: dict[str, SqlAlchemyPeerMessageStore] = {}


def get_peer_message_store() -> SqlAlchemyPeerMessageStore:
    """Return the durable peer-message store (BDP-2270 C2, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _peer_store_cache.get(location)
    if store is None:
        store = SqlAlchemyPeerMessageStore(location)
        _peer_store_cache[location] = store
    return store
