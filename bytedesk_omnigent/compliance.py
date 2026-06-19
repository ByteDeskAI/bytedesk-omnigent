"""Outreach do-not-contact suppression store (BDP-2278 F3, ADR-0142).

The compliance floor for agent-driven outreach: a durable ``(channel, address)``
suppression list (opt-out / GDPR erasure / hard bounce / complaint) the outreach
path consults before sending. Idempotent suppress (composite PK); ``is_suppressed``
is an O(1) lookup. Addresses are normalized (lower + strip) so a casing/whitespace
variant can't slip past the check. Pairs with the stateless CAN-SPAM policy
(``policies/builtins/outreach_compliance.py``). Shares the conversation store's
engine, mirroring the goals/peer store shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.db_models import SqlSuppression
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


@dataclass(frozen=True)
class Suppression:
    """A row of ``suppressions``."""

    channel: str
    address: str
    reason: str
    created_at: int


def _normalize(address: str) -> str:
    return address.strip().lower()


def _to_suppression(row: SqlSuppression) -> Suppression:
    return Suppression(
        channel=row.channel,
        address=row.address,
        reason=row.reason,
        created_at=row.created_at,
    )


class SqlAlchemySuppressionStore:
    """Durable do-not-contact suppression store (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def suppress(
        self, *, channel: str, address: str, reason: str, now: int | None = None
    ) -> bool:
        """Idempotently add a ``(channel, address)`` suppression.

        Returns ``True`` if newly added, ``False`` if it already existed (the
        composite PK makes a duplicate a no-op — a re-opt-out is harmless).
        """
        now = now_epoch() if now is None else now
        normalized = _normalize(address)
        with self._write_session() as session:
            existing = session.get(SqlSuppression, (channel, normalized))
            if existing is not None:
                return False
            session.add(
                SqlSuppression(
                    channel=channel,
                    address=normalized,
                    reason=reason,
                    created_at=now,
                )
            )
            try:
                session.flush()
            except IntegrityError:
                # Lost an insert race — another writer suppressed it first.
                session.rollback()
                return False
            return True

    def is_suppressed(self, *, channel: str, address: str) -> bool:
        """True if ``(channel, address)`` must not be contacted."""
        with self._session() as session:
            return (
                session.get(SqlSuppression, (channel, _normalize(address))) is not None
            )

    def list_suppressed(self, *, channel: str | None = None) -> list[Suppression]:
        """List suppressions, optionally for one channel."""
        stmt = select(SqlSuppression)
        if channel is not None:
            stmt = stmt.where(SqlSuppression.channel == channel)
        stmt = stmt.order_by(SqlSuppression.created_at.desc())
        with self._session() as session:
            return [_to_suppression(r) for r in session.execute(stmt).scalars().all()]


_suppression_store_cache: dict[str, SqlAlchemySuppressionStore] = {}


def get_suppression_store() -> SqlAlchemySuppressionStore:
    """Return the durable do-not-contact suppression store (BDP-2278 F3, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _suppression_store_cache.get(location)
    if store is None:
        store = SqlAlchemySuppressionStore(location)
        _suppression_store_cache[location] = store
    return store
