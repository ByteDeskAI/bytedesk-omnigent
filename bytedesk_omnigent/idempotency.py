"""Durable idempotency-key store (BDP-2251, ADR-0142, aligned ADR-0009/0077).

A generic at-most-once claim plane: a consumer ``claim(scope, key)`` before
doing work; the first caller wins (returns ``True``), a duplicate delivery loses
(returns ``False``). The composite ``(scope, key)`` primary key is the atomic
guard — a duplicate insert hits the PK conflict (caught as ``IntegrityError``).
Replaces the per-consumer ``DbSupportTicketIdempotencyStore`` /
``WorkflowTriggerInboxEntry`` so the event-trigger re-homes (and any redelivered
external event) dedup against one durable plane. Shares the conversation store's
engine, like the signal bus and cron scheduler.
"""

from __future__ import annotations

import json

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlIdempotencyKey
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


class SqlAlchemyIdempotencyStore:
    """Durable at-most-once claim store (ADR-0009 Idempotent Receiver).

    :param storage_location: SQLAlchemy database URI (the same engine the
        conversation store uses).
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        # immediate=True: SQLite write-lock-before-read so the claim insert cannot
        # race (the composite PK is the real guard on both dialects).
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        """The underlying SQLAlchemy engine."""
        return self._engine

    def claim(self, *, scope: str, key: str, now: int | None = None) -> bool:
        """Atomically claim ``(scope, key)``.

        :returns: ``True`` if THIS caller claimed it (new — do the work),
            ``False`` if it was already claimed (duplicate delivery — skip).
        """
        now = now_epoch() if now is None else now
        try:
            with self._write_session() as session:
                session.add(
                    SqlIdempotencyKey(
                        scope=scope, key=key, claimed_at=now, created_at=now
                    )
                )
                session.flush()
            return True
        except IntegrityError:
            return False

    def is_claimed(self, *, scope: str, key: str) -> bool:
        """Whether ``(scope, key)`` has already been claimed."""
        with self._session() as session:
            return session.get(SqlIdempotencyKey, (scope, key)) is not None

    def mark_dead_lettered(
        self, *, scope: str, key: str, result: dict | None = None
    ) -> None:
        """Mark an already-claimed key as dead-lettered (work failed past redelivery)."""
        with self._write_session() as session:
            session.execute(
                update(SqlIdempotencyKey)
                .where(
                    SqlIdempotencyKey.scope == scope,
                    SqlIdempotencyKey.key == key,
                )
                .values(
                    dead_lettered=True,
                    result=json.dumps(result) if result is not None else None,
                )
            )


# Lazily-built, per-URI cache of the idempotency store, mirroring the runtime
# accessors for the signal bus + cron scheduler. Kept module-local (no lifespan
# loop, so no runtime registration needed).
_idempotency_store_cache: dict[str, SqlAlchemyIdempotencyStore] = {}


def get_idempotency_store() -> SqlAlchemyIdempotencyStore:
    """Return the durable idempotency store (BDP-2251, ADR-0142).

    Built lazily from the conversation store's database URI and cached per URI.
    """
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _idempotency_store_cache.get(location)
    if store is None:
        store = SqlAlchemyIdempotencyStore(location)
        _idempotency_store_cache[location] = store
    return store
