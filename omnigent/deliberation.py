"""Native deliberation store (BDP-2273 C6, ADR-0142).

The decision organ: a durable proposal→debate→decision ritual so a company
decides by *proposal + debate*, not one manager's prompt — and "what did we
decide about X?" is a durable query, not lost in a chat scroll. Open a
deliberation on a ``topic`` with a ``proposal``; named peers (routed via the
delegation graph, C1) add positions (for / against / amend) across rounds;
``decide`` records the outcome with a guarded ``open → decided`` transition.
Mirrors the goals/peer single-writer store shape; shares the conversation store's
engine. The ``bytedesk_deliberation_*`` tools + the weekly strategy cron are the
integration follow-up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update

from omnigent.db.db_models import SqlDeliberation, SqlDeliberationPosition
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class Deliberation:
    """A row of ``deliberations``."""

    id: str
    topic: str
    proposal: str
    status: str
    decision: str | None
    decided_by: str | None
    opened_by: str | None
    created_at: int
    decided_at: int | None


@dataclass(frozen=True)
class Position:
    """A row of ``deliberation_positions``."""

    id: str
    deliberation_id: str
    agent_id: str
    stance: str
    body: str
    round: int
    created_at: int


def _to_delib(row: SqlDeliberation) -> Deliberation:
    return Deliberation(
        id=row.id,
        topic=row.topic,
        proposal=row.proposal,
        status=row.status,
        decision=row.decision,
        decided_by=row.decided_by,
        opened_by=row.opened_by,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


def _to_position(row: SqlDeliberationPosition) -> Position:
    return Position(
        id=row.id,
        deliberation_id=row.deliberation_id,
        agent_id=row.agent_id,
        stance=row.stance,
        body=row.body,
        round=row.round,
        created_at=row.created_at,
    )


class SqlAlchemyDeliberationStore:
    """Durable proposal→debate→decision store (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def start(
        self,
        *,
        topic: str,
        proposal: str,
        opened_by: str | None = None,
        now: int | None = None,
    ) -> Deliberation:
        """Open a deliberation on ``topic`` with the opening ``proposal``."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlDeliberation(
                id=f"delib_{uuid.uuid4().hex}",
                topic=topic,
                proposal=proposal,
                status="open",
                opened_by=opened_by,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_delib(row)

    def add_position(
        self,
        *,
        deliberation_id: str,
        agent_id: str,
        stance: str,
        body: str,
        round: int = 1,
        now: int | None = None,
    ) -> Position:
        """Record a position (``for`` / ``against`` / ``amend``) in a round."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlDeliberationPosition(
                id=f"pos_{uuid.uuid4().hex}",
                deliberation_id=deliberation_id,
                agent_id=agent_id,
                stance=stance,
                body=body,
                round=round,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_position(row)

    def decide(
        self,
        *,
        deliberation_id: str,
        decision: str,
        decided_by: str,
        now: int | None = None,
    ) -> bool:
        """Guarded ``open → decided`` with the recorded ``decision``.

        Returns ``True`` when this caller closed the open deliberation
        (``rowcount==1``), ``False`` if it was already decided/closed.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            res = session.execute(
                update(SqlDeliberation)
                .where(
                    SqlDeliberation.id == deliberation_id,
                    SqlDeliberation.status == "open",
                )
                .values(
                    status="decided",
                    decision=decision,
                    decided_by=decided_by,
                    decided_at=now,
                )
            )
            return res.rowcount == 1

    def positions(self, *, deliberation_id: str) -> list[Position]:
        """Return positions for a deliberation, ordered by round then time."""
        stmt = (
            select(SqlDeliberationPosition)
            .where(SqlDeliberationPosition.deliberation_id == deliberation_id)
            .order_by(
                SqlDeliberationPosition.round, SqlDeliberationPosition.created_at
            )
        )
        with self._session() as session:
            return [_to_position(r) for r in session.execute(stmt).scalars().all()]

    def get(self, *, deliberation_id: str) -> Deliberation | None:
        """Return a deliberation (or ``None``)."""
        with self._session() as session:
            row = session.get(SqlDeliberation, deliberation_id)
            return _to_delib(row) if row is not None else None

    def find_decision(self, *, topic: str) -> Deliberation | None:
        """"What did we decide about X?" — the latest decided deliberation on ``topic``."""
        stmt = (
            select(SqlDeliberation)
            .where(
                SqlDeliberation.topic == topic,
                SqlDeliberation.status == "decided",
            )
            .order_by(SqlDeliberation.decided_at.desc())
            .limit(1)
        )
        with self._session() as session:
            row = session.execute(stmt).scalar_one_or_none()
            return _to_delib(row) if row is not None else None

    def list_open(self) -> list[Deliberation]:
        """Return open deliberations (oldest first)."""
        stmt = (
            select(SqlDeliberation)
            .where(SqlDeliberation.status == "open")
            .order_by(SqlDeliberation.created_at)
        )
        with self._session() as session:
            return [_to_delib(r) for r in session.execute(stmt).scalars().all()]


_deliberation_store_cache: dict[str, SqlAlchemyDeliberationStore] = {}


def get_deliberation_store() -> SqlAlchemyDeliberationStore:
    """Return the durable deliberation store (BDP-2273 C6, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _deliberation_store_cache.get(location)
    if store is None:
        store = SqlAlchemyDeliberationStore(location)
        _deliberation_store_cache[location] = store
    return store
