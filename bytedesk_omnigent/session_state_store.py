"""Session-state store — an additive facade over the existing
``conversations.session_state`` / ``conversations.session_usage``
columns (Phase 6d, BDP-2342, ADR-0143).

The conversation store already owns these two JSON-in-Text columns and
keeps writing them through ``set_session_state`` / ``set_session_usage``.
This store is a **read/write facade** that lets ByteDesk extension code
(policy callables, budget hard-stops, the org engine) reach the
per-conversation key/value state and cumulative token-usage blobs without
loading a full :class:`~omnigent.entities.Conversation`.

It introduces **no new table** — it wraps the existing columns on the
``conversations`` table — and follows the ByteDesk extension store shape
(ABC + ``SqlAlchemy*`` impl + ``sql_*_to_*`` converter, sharing the
conversation store's engine exactly like the signal bus, cron scheduler,
and tool-step store). ``conversation_id`` is a plain column read (a soft
reference, no hard FK added) so this store stays decoupled and
unit-provable standalone. JSON is stored as Text (never native JSONB) for
SQLite/PostgreSQL parity.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from omnigent.db.db_models import SqlConversation
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
)


@dataclass(frozen=True)
class SessionStateSnapshot:
    """A read-only snapshot of a conversation's session-state facade.

    Decodes both JSON-in-Text columns into dicts. An absent/``NULL``
    column decodes to an empty dict, mirroring the
    :class:`~omnigent.entities.Conversation` entity (where both fields
    ``default_factory=dict``), so callers never branch on ``None``.

    :param conversation_id: The conversation (session) id this snapshot
        belongs to, e.g. ``"conv_abc123"``.
    :param state: The mutable per-conversation key/value store that
        policy callables accumulate across turns.
    :param usage: The cumulative LLM token-usage blob, e.g.
        ``{"input_tokens": 1500, "output_tokens": 350,
        "total_tokens": 1850}``; may carry a nested ``"by_model"`` map.
    """

    conversation_id: str
    state: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


def sql_conversation_to_session_state(row: SqlConversation) -> SessionStateSnapshot:
    """Convert a :class:`SqlConversation` ORM row to a
    :class:`SessionStateSnapshot`.

    Reads only the two facade columns; everything else on the row is
    ignored. A ``NULL`` (or empty) column decodes to ``{}``.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`SessionStateSnapshot` dataclass instance.
    """
    state: dict[str, Any] = json.loads(row.session_state) if row.session_state else {}
    usage: dict[str, Any] = json.loads(row.session_usage) if row.session_usage else {}
    return SessionStateSnapshot(
        conversation_id=row.id,
        state=state,
        usage=usage,
    )


class SessionStateStore(ABC):
    """Abstract base for the session-state facade.

    Exposes the per-conversation ``session_state`` / ``session_usage``
    blobs as first-class read/write operations over the existing
    ``conversations`` columns. The conversation store remains the
    authority for the rows themselves (create/delete); this facade only
    reads and overwrites the two JSON columns.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the session-state store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get_snapshot(self, conversation_id: str) -> SessionStateSnapshot | None:
        """Return the full state+usage snapshot, or ``None`` if the
        conversation does not exist.

        :param conversation_id: The conversation (session) id,
            e.g. ``"conv_abc123"``.
        :returns: A :class:`SessionStateSnapshot`, or ``None`` when no
            such conversation row exists.
        """
        ...

    @abstractmethod
    def get_state(self, conversation_id: str) -> dict[str, Any]:
        """Return the decoded ``session_state`` dict for a conversation.

        A missing conversation, or a ``NULL`` column, both yield an empty
        dict — callers never branch on ``None``.

        :param conversation_id: The conversation (session) id,
            e.g. ``"conv_abc123"``.
        :returns: The decoded session-state dict (``{}`` when unset).
        """
        ...

    @abstractmethod
    def set_state(self, conversation_id: str, state: dict[str, Any]) -> None:
        """Persist the full session-state snapshot for a conversation.

        Serializes *state* as JSON and overwrites the ``session_state``
        column. A no-op when the conversation does not exist (the guarded
        UPDATE matches zero rows).

        :param conversation_id: The conversation (session) id to update,
            e.g. ``"conv_abc123"``.
        :param state: The complete session-state dict to persist.
        """
        ...

    @abstractmethod
    def get_usage(self, conversation_id: str) -> dict[str, Any]:
        """Return the decoded ``session_usage`` dict for a conversation.

        A missing conversation, or a ``NULL`` column, both yield an empty
        dict.

        :param conversation_id: The conversation (session) id,
            e.g. ``"conv_abc123"``.
        :returns: The decoded cumulative-usage dict (``{}`` when unset).
        """
        ...

    @abstractmethod
    def set_usage(self, conversation_id: str, usage: dict[str, Any]) -> None:
        """Persist the cumulative LLM token usage for a conversation.

        Serializes *usage* as JSON and overwrites the ``session_usage``
        column. A no-op when the conversation does not exist.

        :param conversation_id: The conversation (session) id to update,
            e.g. ``"conv_abc123"``.
        :param usage: The complete usage dict to persist, e.g.
            ``{"input_tokens": 1500, "output_tokens": 350,
            "total_tokens": 1850}``. May carry a nested ``"by_model"``
            sub-dict, hence ``Any``.
        """
        ...


class SqlAlchemySessionStateStore(SessionStateStore):
    """SQLAlchemy-backed :class:`SessionStateStore`.

    Reads/writes the existing ``conversations.session_state`` and
    ``conversations.session_usage`` JSON-in-Text columns — no new table.
    Shares the conversation store's engine + managed session maker, the
    same shape as the durable signal bus / cron scheduler / tool-step
    store.

    :param storage_location: SQLAlchemy database URI (the same engine the
        conversation store uses), e.g. ``"sqlite:///omnigent.db"`` or
        ``"postgresql://user:pass@host/db"``.
    """

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    @property
    def engine(self):
        """The underlying SQLAlchemy engine (shared with the conversation store)."""
        return self._engine

    def get_snapshot(self, conversation_id: str) -> SessionStateSnapshot | None:
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            return sql_conversation_to_session_state(row) if row is not None else None

    def get_state(self, conversation_id: str) -> dict[str, Any]:
        with self._session() as session:
            raw = session.execute(
                select(SqlConversation.session_state).where(
                    SqlConversation.id == conversation_id
                )
            ).scalar_one_or_none()
            return json.loads(raw) if raw else {}

    def set_state(self, conversation_id: str, state: dict[str, Any]) -> None:
        with self._session() as session:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id == conversation_id)
                .values(session_state=json.dumps(state))
            )

    def get_usage(self, conversation_id: str) -> dict[str, Any]:
        with self._session() as session:
            raw = session.execute(
                select(SqlConversation.session_usage).where(
                    SqlConversation.id == conversation_id
                )
            ).scalar_one_or_none()
            return json.loads(raw) if raw else {}

    def set_usage(self, conversation_id: str, usage: dict[str, Any]) -> None:
        with self._session() as session:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id == conversation_id)
                .values(session_usage=json.dumps(usage))
            )
