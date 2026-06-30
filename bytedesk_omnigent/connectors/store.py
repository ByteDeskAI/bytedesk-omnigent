"""Durable connector store."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update

from bytedesk_omnigent.db_models import (
    SqlConnectorAgentGrant,
    SqlConnectorConnection,
    SqlConnectorOAuthState,
    SqlConnectorService,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConnectorConnection:
    id: str
    provider: str
    display_name: str
    auth_type: str
    status: str
    scopes: list[str]
    metadata: dict[str, Any]
    secret_ref: str | None
    last_health_status: str | None
    last_health_at: int | None
    last_error: str | None
    created_at: int
    updated_at: int
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "displayName": self.display_name,
            "authType": self.auth_type,
            "status": self.status,
            "scopes": list(self.scopes),
            "metadata": dict(self.metadata),
            "secretPresent": self.secret_ref is not None,
            "lastHealthStatus": self.last_health_status,
            "lastHealthAt": self.last_health_at,
            "lastError": self.last_error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
        }


@dataclass(frozen=True)
class ConnectorServiceState:
    id: str
    connection_id: str
    service_key: str
    enabled: bool
    status: str
    scopes: list[str]
    metadata: dict[str, Any]
    updated_at: int
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connectionId": self.connection_id,
            "serviceKey": self.service_key,
            "enabled": self.enabled,
            "status": self.status,
            "scopes": list(self.scopes),
            "metadata": dict(self.metadata),
            "updatedAt": self.updated_at,
            "version": self.version,
        }


@dataclass(frozen=True)
class ConnectorAgentGrant:
    id: str
    connection_id: str
    agent_id: str
    service_key: str
    tool_key: str
    enabled: bool
    status: str
    metadata: dict[str, Any]
    created_at: int
    updated_at: int
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connectionId": self.connection_id,
            "agentId": self.agent_id,
            "serviceKey": self.service_key,
            "toolKey": self.tool_key,
            "enabled": self.enabled,
            "status": self.status,
            "metadata": dict(self.metadata),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
        }


@dataclass(frozen=True)
class ConnectorOAuthState:
    id: str
    provider: str
    requested_scopes: list[str]
    redirect_uri: str
    code_verifier: str | None
    metadata: dict[str, Any]
    expires_at: int
    consumed_at: int | None
    created_at: int


def _connection(row: SqlConnectorConnection) -> ConnectorConnection:
    return ConnectorConnection(
        id=row.id,
        provider=row.provider,
        display_name=row.display_name,
        auth_type=row.auth_type,
        status=row.status,
        scopes=_json_loads(row.scopes, []),
        metadata=_json_loads(row.meta, {}),
        secret_ref=row.secret_ref,
        last_health_status=row.last_health_status,
        last_health_at=row.last_health_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _service(row: SqlConnectorService) -> ConnectorServiceState:
    return ConnectorServiceState(
        id=row.id,
        connection_id=row.connection_id,
        service_key=row.service_key,
        enabled=row.enabled,
        status=row.status,
        scopes=_json_loads(row.scopes, []),
        metadata=_json_loads(row.meta, {}),
        updated_at=row.updated_at,
        version=row.version,
    )


def _grant(row: SqlConnectorAgentGrant) -> ConnectorAgentGrant:
    return ConnectorAgentGrant(
        id=row.id,
        connection_id=row.connection_id,
        agent_id=row.agent_id,
        service_key=row.service_key,
        tool_key=row.tool_key,
        enabled=row.enabled,
        status=row.status,
        metadata=_json_loads(row.meta, {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _state(row: SqlConnectorOAuthState) -> ConnectorOAuthState:
    return ConnectorOAuthState(
        id=row.id,
        provider=row.provider,
        requested_scopes=_json_loads(row.requested_scopes, []),
        redirect_uri=row.redirect_uri,
        code_verifier=row.code_verifier,
        metadata=_json_loads(row.meta, {}),
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )


class SqlAlchemyConnectorStore:
    """Connector source of truth backed by the Omnigent database."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def list_connections(self, *, provider: str | None = None) -> list[ConnectorConnection]:
        stmt = select(SqlConnectorConnection)
        if provider is not None:
            stmt = stmt.where(SqlConnectorConnection.provider == provider)
        stmt = stmt.order_by(SqlConnectorConnection.provider, SqlConnectorConnection.display_name)
        with self._session() as session:
            return [_connection(r) for r in session.execute(stmt).scalars().all()]

    def get_connection(self, connection_id: str) -> ConnectorConnection | None:
        with self._session() as session:
            row = session.get(SqlConnectorConnection, connection_id)
            return _connection(row) if row is not None else None

    def upsert_connection(
        self,
        *,
        provider: str,
        display_name: str,
        auth_type: str,
        scopes: list[str],
        metadata: dict[str, Any] | None = None,
        secret_ref: str | None = None,
        status: str = "connected",
        connection_id: str | None = None,
        now: int | None = None,
    ) -> ConnectorConnection:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlConnectorConnection, connection_id) if connection_id else None
            if row is None:
                row = SqlConnectorConnection(
                    id=connection_id or _new_id("conn"),
                    provider=provider,
                    display_name=display_name,
                    auth_type=auth_type,
                    status=status,
                    scopes=_json_dumps(scopes),
                    secret_ref=secret_ref,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.display_name = display_name
                row.auth_type = auth_type
                row.status = status
                row.scopes = _json_dumps(scopes)
                row.secret_ref = secret_ref
                row.updated_at = now
                row.version += 1
                row.meta = _json_dumps(metadata or {})
            session.flush()
            return _connection(row)

    def update_health(
        self,
        connection_id: str,
        *,
        status: str,
        error: str | None = None,
        now: int | None = None,
    ) -> ConnectorConnection | None:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlConnectorConnection, connection_id)
            if row is None:
                return None
            row.last_health_status = status
            row.last_health_at = now
            row.last_error = error
            row.updated_at = now
            row.version += 1
            session.flush()
            return _connection(row)

    def upsert_service(
        self,
        *,
        connection_id: str,
        service_key: str,
        enabled: bool,
        scopes: list[str] | None = None,
        status: str = "ready",
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> ConnectorServiceState:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.execute(
                select(SqlConnectorService).where(
                    SqlConnectorService.connection_id == connection_id,
                    SqlConnectorService.service_key == service_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlConnectorService(
                    id=_new_id("csvc"),
                    connection_id=connection_id,
                    service_key=service_key,
                    enabled=enabled,
                    status=status,
                    scopes=_json_dumps(scopes or []),
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.status = status
                row.scopes = _json_dumps(scopes or _json_loads(row.scopes, []))
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
                row.updated_at = now
                row.version += 1
            session.flush()
            return _service(row)

    def list_services(self, connection_id: str) -> list[ConnectorServiceState]:
        stmt = (
            select(SqlConnectorService)
            .where(SqlConnectorService.connection_id == connection_id)
            .order_by(SqlConnectorService.service_key)
        )
        with self._session() as session:
            return [_service(r) for r in session.execute(stmt).scalars().all()]

    def set_service_enabled(
        self, connection_id: str, service_key: str, enabled: bool
    ) -> ConnectorServiceState | None:
        with self._write_session() as session:
            row = session.execute(
                select(SqlConnectorService).where(
                    SqlConnectorService.connection_id == connection_id,
                    SqlConnectorService.service_key == service_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.enabled = enabled
            row.status = "ready" if enabled else "disabled"
            row.updated_at = now_epoch()
            row.version += 1
            session.flush()
            return _service(row)

    def upsert_agent_grant(
        self,
        *,
        connection_id: str,
        agent_id: str,
        service_key: str,
        tool_key: str,
        enabled: bool,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> ConnectorAgentGrant:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.execute(
                select(SqlConnectorAgentGrant).where(
                    SqlConnectorAgentGrant.connection_id == connection_id,
                    SqlConnectorAgentGrant.agent_id == agent_id,
                    SqlConnectorAgentGrant.service_key == service_key,
                    SqlConnectorAgentGrant.tool_key == tool_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlConnectorAgentGrant(
                    id=_new_id("cgrant"),
                    connection_id=connection_id,
                    agent_id=agent_id,
                    service_key=service_key,
                    tool_key=tool_key,
                    enabled=enabled,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.status = status
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
                row.updated_at = now
                row.version += 1
            session.flush()
            return _grant(row)

    def list_agent_grants(
        self,
        *,
        connection_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[ConnectorAgentGrant]:
        stmt = select(SqlConnectorAgentGrant)
        if connection_id is not None:
            stmt = stmt.where(SqlConnectorAgentGrant.connection_id == connection_id)
        if agent_id is not None:
            stmt = stmt.where(SqlConnectorAgentGrant.agent_id == agent_id)
        stmt = stmt.order_by(
            SqlConnectorAgentGrant.agent_id,
            SqlConnectorAgentGrant.service_key,
            SqlConnectorAgentGrant.tool_key,
        )
        with self._session() as session:
            return [_grant(r) for r in session.execute(stmt).scalars().all()]

    def delete_agent_grant(self, grant_id: str) -> bool:
        with self._write_session() as session:
            result = session.execute(
                delete(SqlConnectorAgentGrant).where(SqlConnectorAgentGrant.id == grant_id)
            )
            return result.rowcount == 1

    def create_oauth_state(
        self,
        *,
        state: str,
        provider: str,
        requested_scopes: list[str],
        redirect_uri: str,
        code_verifier: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int = 600,
        now: int | None = None,
    ) -> ConnectorOAuthState:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = SqlConnectorOAuthState(
                id=_new_id("cstate"),
                state_hash=hash_oauth_state(state),
                provider=provider,
                requested_scopes=_json_dumps(requested_scopes),
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                expires_at=now + ttl_seconds,
                consumed_at=None,
                created_at=now,
                meta=_json_dumps(metadata or {}),
            )
            session.add(row)
            session.flush()
            return _state(row)

    def consume_oauth_state(
        self,
        state: str,
        *,
        now: int | None = None,
    ) -> ConnectorOAuthState | None:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.execute(
                select(SqlConnectorOAuthState).where(
                    SqlConnectorOAuthState.state_hash == hash_oauth_state(state)
                )
            ).scalar_one_or_none()
            if row is None or row.consumed_at is not None or row.expires_at < now:
                return None
            session.execute(
                update(SqlConnectorOAuthState)
                .where(SqlConnectorOAuthState.id == row.id)
                .values(consumed_at=now)
            )
            session.flush()
            row.consumed_at = now
            return _state(row)


_connector_store_cache: dict[str, SqlAlchemyConnectorStore] = {}


def get_connector_store() -> SqlAlchemyConnectorStore:
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _connector_store_cache.get(location)
    if store is None:
        store = SqlAlchemyConnectorStore(location)
        _connector_store_cache[location] = store
    return store
