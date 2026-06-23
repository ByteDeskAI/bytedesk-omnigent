"""SQLAlchemy-backed policy store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlPolicy
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Policy
from omnigent.stores.policy_store import PolicyStore


def _to_entity(row: SqlPolicy) -> Policy:
    """
    Convert a :class:`SqlPolicy` ORM row to a :class:`Policy` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Policy` dataclass instance.
    """
    return Policy(
        id=row.id,
        name=row.name,
        session_id=row.session_id,
        created_at=row.created_at,
        type=row.type,
        handler=row.handler,
        factory_params=json.loads(row.factory_params) if row.factory_params else None,
        enabled=bool(row.enabled),
        updated_at=row.updated_at,
        created_by=row.created_by,
        version=row.version,
    )


class SqlAlchemyPolicyStore(PolicyStore):
    """
    SQLAlchemy-backed implementation of :class:`PolicyStore`.

    Persists policies in a relational database via SQLAlchemy ORM.
    Supports both session-scoped (``session_id`` set) and
    server-wide default (``session_id IS NULL``) policies.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy policy store.

        Creates or reuses a SQLAlchemy engine and session
        factory for the given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # ── Session-scoped policy methods ────────────────────────────

    def create(
        self,
        policy_id: str,
        session_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> Policy:
        """Insert a new session-scoped policy.

        Raises ``IntegrityError`` on ``(session_id, name)`` collision.
        """
        row = SqlPolicy(
            id=policy_id,
            name=name,
            session_id=session_id,
            created_at=now_epoch(),
            updated_at=None,
            type=type,
            handler=handler,
            factory_params=json.dumps(factory_params) if factory_params else None,
            enabled=enabled,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, policy_id: str, session_id: str) -> Policy | None:
        """Return the policy if it belongs to the given session."""
        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id != session_id:
                return None
            return _to_entity(row)

    def list_for_session(self, session_id: str) -> list[Policy]:
        """List policies for a session ordered by ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlPolicy)
                .where(SqlPolicy.session_id == session_id)
                .order_by(asc(SqlPolicy.created_at), asc(SqlPolicy.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        policy_id: str,
        session_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
        expected_version: int | None = None,
    ) -> Policy | None:
        """
        Update mutable fields. Returns ``None`` if not found or
        wrong session.

        Optimistic concurrency (BDP-2412): when *expected_version* is
        given the write is a guarded compare-and-swap on ``version``
        (raises ``StaleWriteError`` if the row moved); omitted keeps the
        unconditional update. A real change bumps ``version`` either way.
        """
        from sqlalchemy import update as sql_update

        from omnigent.errors import StaleWriteError

        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id != session_id:
                return None
            if expected_version is None:
                changed = False
                if name is not None and row.name != name:
                    row.name = name
                    changed = True
                if handler is not None and row.handler != handler:
                    row.handler = handler
                    changed = True
                if enabled is not None and bool(row.enabled) != enabled:
                    row.enabled = enabled
                    changed = True
                if changed:
                    row.version = row.version + 1
                    row.updated_at = now_epoch()
                session.flush()
                return _to_entity(row)
            # Guarded compare-and-swap on version (ownership already checked).
            values: dict[str, Any] = {}
            if name is not None and row.name != name:
                values["name"] = name
            if handler is not None and row.handler != handler:
                values["handler"] = handler
            if enabled is not None and bool(row.enabled) != enabled:
                values["enabled"] = enabled
            if not values:
                return _to_entity(row)  # no-op: nothing to change, no bump
            values["version"] = SqlPolicy.version + 1
            values["updated_at"] = now_epoch()
            result = session.execute(
                sql_update(SqlPolicy)
                .where(SqlPolicy.id == policy_id, SqlPolicy.version == expected_version)
                .values(**values)
            )
            if result.rowcount == 1:
                session.expire_all()
                return _to_entity(session.get(SqlPolicy, policy_id))
            raise StaleWriteError(
                f"policy {policy_id!r} was modified concurrently "
                f"(If-Match version {expected_version} is stale)"
            )

    def delete(self, policy_id: str, session_id: str) -> bool:
        """Delete a policy. Idempotent: returns ``False`` if not found."""
        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id != session_id:
                return False
            session.delete(row)
            return True

    # ── Default (server-wide) policy methods ─────────────────────

    def create_default(
        self,
        policy_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
        created_by: str | None = None,
    ) -> Policy:
        """Insert a new default policy (``session_id=NULL``).

        Raises ``IntegrityError`` on name collision among defaults.

        SQLite treats NULLs as distinct in composite unique
        constraints, so the ``(session_id, name)`` constraint
        does not enforce uniqueness among default policies.
        This method checks for duplicates explicitly.
        """

        row = SqlPolicy(
            id=policy_id,
            name=name,
            session_id=None,
            created_at=now_epoch(),
            updated_at=None,
            type=type,
            handler=handler,
            factory_params=json.dumps(factory_params) if factory_params else None,
            enabled=enabled,
            created_by=created_by,
        )
        with self._session() as session:
            # Explicit uniqueness check: SQLite treats NULLs as
            # distinct in composite unique constraints, so
            # (NULL, name) won't collide with another (NULL, name).
            existing = (
                session.execute(
                    select(SqlPolicy)
                    .where(SqlPolicy.session_id.is_(None))
                    .where(SqlPolicy.name == name)
                )
                .scalars()
                .first()
            )
            if existing is not None:
                raise IntegrityError(
                    "Duplicate default policy name",
                    params={"name": name},
                    orig=Exception(f"UNIQUE constraint: name={name!r}"),
                )
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get_default(self, policy_id: str) -> Policy | None:
        """Return a default policy by ID (``session_id IS NULL``)."""
        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id is not None:
                return None
            return _to_entity(row)

    def list_defaults(self) -> list[Policy]:
        """List all default policies ordered by ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlPolicy)
                .where(SqlPolicy.session_id.is_(None))
                .order_by(asc(SqlPolicy.created_at), asc(SqlPolicy.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update_default(
        self,
        policy_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
        expected_version: int | None = None,
    ) -> Policy | None:
        """
        Update mutable fields of a default policy. Returns ``None``
        if not found or not a default policy.

        Optimistic concurrency (BDP-2412): when *expected_version* is
        given the write is a guarded compare-and-swap on ``version``
        (raises ``StaleWriteError`` on a stale ETag); the name-uniqueness
        check runs BEFORE the guarded UPDATE. Omitted keeps the
        unconditional update; a real change bumps ``version`` either way.
        """
        from sqlalchemy import update as sql_update

        from omnigent.errors import StaleWriteError

        def _check_name_unique(session: Any, new_name: str) -> None:
            # SQLite treats NULLs as distinct, so the composite constraint
            # won't catch (NULL, name) collisions — check explicitly.
            conflict = (
                session.execute(
                    select(SqlPolicy)
                    .where(SqlPolicy.session_id.is_(None))
                    .where(SqlPolicy.name == new_name)
                    .where(SqlPolicy.id != policy_id)
                )
                .scalars()
                .first()
            )
            if conflict is not None:
                raise IntegrityError(
                    "Duplicate default policy name",
                    params={"name": new_name},
                    orig=Exception(f"UNIQUE constraint: name={new_name!r}"),
                )

        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id is not None:
                return None
            if expected_version is None:
                changed = False
                if name is not None and row.name != name:
                    _check_name_unique(session, name)
                    row.name = name
                    changed = True
                if handler is not None and row.handler != handler:
                    row.handler = handler
                    changed = True
                if enabled is not None and bool(row.enabled) != enabled:
                    row.enabled = enabled
                    changed = True
                if changed:
                    row.version = row.version + 1
                    row.updated_at = now_epoch()
                session.flush()
                return _to_entity(row)
            # Guarded path: uniqueness FIRST, then compare-and-swap on version.
            values: dict[str, Any] = {}
            if name is not None and row.name != name:
                _check_name_unique(session, name)
                values["name"] = name
            if handler is not None and row.handler != handler:
                values["handler"] = handler
            if enabled is not None and bool(row.enabled) != enabled:
                values["enabled"] = enabled
            if not values:
                return _to_entity(row)
            values["version"] = SqlPolicy.version + 1
            values["updated_at"] = now_epoch()
            result = session.execute(
                sql_update(SqlPolicy)
                .where(SqlPolicy.id == policy_id, SqlPolicy.version == expected_version)
                .values(**values)
            )
            if result.rowcount == 1:
                session.expire_all()
                return _to_entity(session.get(SqlPolicy, policy_id))
            raise StaleWriteError(
                f"default policy {policy_id!r} was modified concurrently "
                f"(If-Match version {expected_version} is stale)"
            )

    def delete_default(self, policy_id: str) -> bool:
        """Delete a default policy. Idempotent."""
        with self._session() as session:
            row = session.get(SqlPolicy, policy_id)
            if row is None or row.session_id is not None:
                return False
            session.delete(row)
            return True
