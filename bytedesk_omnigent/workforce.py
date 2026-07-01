"""Work Force inheritance store, resolver, and materialization helpers."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from bytedesk_omnigent.db_models import (
    SqlWorkforceAgentMaterialization,
    SqlWorkforceAgentOverride,
    SqlWorkforceConnectorAssignment,
    SqlWorkforceInstruction,
    SqlWorkforceRevision,
    SqlWorkforceSkillAssignment,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch

logger = logging.getLogger(__name__)

ScopeKind = Literal["organization", "department", "agent"]
InheritedScopeKind = Literal["organization", "department"]
ItemKind = Literal["connector", "skill"]

ORG_SCOPE_ID = "organization"
REVISION_ID = "workforce"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def slug(value: str | None) -> str | None:
    """Normalize a free-form Work Force label to a stable slug."""
    if value is None:
        return None
    cleaned = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return cleaned or None


def normalize_scope(scope_kind: str, scope_id: str | None = None) -> tuple[ScopeKind, str]:
    """Normalize scope values accepted from API/UI callers."""
    if scope_kind not in {"organization", "department", "agent"}:
        raise ValueError(f"unsupported workforce scope kind: {scope_kind!r}")
    if scope_kind == "organization":
        return "organization", ORG_SCOPE_ID
    if not scope_id or not scope_id.strip():
        raise ValueError(f"{scope_kind} scope_id is required")
    normalized = slug(scope_id) if scope_kind == "department" else scope_id.strip()
    if not normalized:
        raise ValueError(f"{scope_kind} scope_id is required")
    return scope_kind, normalized


def connector_item_key(connection_id: str, service_key: str, tool_key: str) -> str:
    return f"{connection_id}:{service_key}:{tool_key}"


def parse_connector_item_key(item_key: str) -> tuple[str, str, str] | None:
    parts = item_key.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


@dataclass(frozen=True)
class WorkforceInstruction:
    id: str
    scope_kind: ScopeKind
    scope_id: str
    body: str
    enabled: bool
    created_at: int
    updated_at: int
    version: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopeKind": self.scope_kind,
            "scopeId": self.scope_id,
            "body": self.body,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkforceConnectorAssignment:
    id: str
    scope_kind: InheritedScopeKind
    scope_id: str
    connection_id: str
    service_key: str
    tool_key: str
    enabled: bool
    created_at: int
    updated_at: int
    version: int
    metadata: dict[str, Any]

    @property
    def item_key(self) -> str:
        return connector_item_key(self.connection_id, self.service_key, self.tool_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopeKind": self.scope_kind,
            "scopeId": self.scope_id,
            "connectionId": self.connection_id,
            "serviceKey": self.service_key,
            "toolKey": self.tool_key,
            "itemKey": self.item_key,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkforceSkillAssignment:
    id: str
    scope_kind: InheritedScopeKind
    scope_id: str
    skill_name: str
    source: str
    source_ref: str | None
    enabled: bool
    created_at: int
    updated_at: int
    version: int
    metadata: dict[str, Any]

    @property
    def item_key(self) -> str:
        return self.skill_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopeKind": self.scope_kind,
            "scopeId": self.scope_id,
            "skillName": self.skill_name,
            "source": self.source,
            "sourceRef": self.source_ref,
            "itemKey": self.item_key,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkforceAgentOverride:
    id: str
    agent_id: str
    item_kind: ItemKind
    item_key: str
    enabled: bool
    created_at: int
    updated_at: int
    version: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agentId": self.agent_id,
            "itemKind": self.item_kind,
            "itemKey": self.item_key,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "version": self.version,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkforceAgentMaterialization:
    id: str
    agent_id: str
    item_kind: ItemKind
    item_key: str
    active: bool
    created_at: int
    updated_at: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agentId": self.agent_id,
            "itemKind": self.item_kind,
            "itemKey": self.item_key,
            "active": self.active,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentWorkforceContext:
    agent_id: str
    agent_name: str
    category: str
    bundle_location: str
    department: str | None
    department_slug: str | None

    @property
    def inheritable(self) -> bool:
        return self.category == "employee" and self.department_slug is not None


def _instruction(row: SqlWorkforceInstruction) -> WorkforceInstruction:
    return WorkforceInstruction(
        id=row.id,
        scope_kind=row.scope_kind,  # type: ignore[arg-type]
        scope_id=row.scope_id,
        body=row.body,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        metadata=_json_loads(row.meta, {}),
    )


def _connector(row: SqlWorkforceConnectorAssignment) -> WorkforceConnectorAssignment:
    return WorkforceConnectorAssignment(
        id=row.id,
        scope_kind=row.scope_kind,  # type: ignore[arg-type]
        scope_id=row.scope_id,
        connection_id=row.connection_id,
        service_key=row.service_key,
        tool_key=row.tool_key,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        metadata=_json_loads(row.meta, {}),
    )


def _skill(row: SqlWorkforceSkillAssignment) -> WorkforceSkillAssignment:
    return WorkforceSkillAssignment(
        id=row.id,
        scope_kind=row.scope_kind,  # type: ignore[arg-type]
        scope_id=row.scope_id,
        skill_name=row.skill_name,
        source=row.source,
        source_ref=row.source_ref,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        metadata=_json_loads(row.meta, {}),
    )


def _override(row: SqlWorkforceAgentOverride) -> WorkforceAgentOverride:
    return WorkforceAgentOverride(
        id=row.id,
        agent_id=row.agent_id,
        item_kind=row.item_kind,  # type: ignore[arg-type]
        item_key=row.item_key,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        metadata=_json_loads(row.meta, {}),
    )


def _materialization(row: SqlWorkforceAgentMaterialization) -> WorkforceAgentMaterialization:
    return WorkforceAgentMaterialization(
        id=row.id,
        agent_id=row.agent_id,
        item_kind=row.item_kind,  # type: ignore[arg-type]
        item_key=row.item_key,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=_json_loads(row.meta, {}),
    )


class SqlAlchemyWorkforceStore:
    """Durable Work Force configuration store."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def revision(self) -> int:
        with self._session() as session:
            row = session.get(SqlWorkforceRevision, REVISION_ID)
            return int(row.version) if row is not None else 0

    def _bump_revision(self, session, now: int) -> None:
        row = session.get(SqlWorkforceRevision, REVISION_ID)
        if row is None:
            session.add(SqlWorkforceRevision(id=REVISION_ID, version=1, updated_at=now))
        else:
            row.version += 1
            row.updated_at = now
        clear_effective_cache()

    def get_instruction(
        self,
        scope_kind: str,
        scope_id: str | None = None,
    ) -> WorkforceInstruction | None:
        kind, sid = normalize_scope(scope_kind, scope_id)
        with self._session() as session:
            row = session.execute(
                select(SqlWorkforceInstruction).where(
                    SqlWorkforceInstruction.scope_kind == kind,
                    SqlWorkforceInstruction.scope_id == sid,
                )
            ).scalar_one_or_none()
            return _instruction(row) if row is not None else None

    def set_instruction(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        body: str,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceInstruction:
        kind, sid = normalize_scope(scope_kind, scope_id)
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceInstruction).where(
                    SqlWorkforceInstruction.scope_kind == kind,
                    SqlWorkforceInstruction.scope_id == sid,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceInstruction(
                    id=_new_id("wfinst"),
                    scope_kind=kind,
                    scope_id=sid,
                    body=body,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.body = body
                row.enabled = enabled
                row.updated_at = now
                row.version += 1
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
            self._bump_revision(session, now)
            session.flush()
            return _instruction(row)

    def list_connector_assignments(
        self,
        *,
        scope_kind: str | None = None,
        scope_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[WorkforceConnectorAssignment]:
        stmt = select(SqlWorkforceConnectorAssignment)
        if scope_kind is not None:
            kind, sid = normalize_scope(scope_kind, scope_id)
            if kind == "agent":
                raise ValueError("connector assignments do not support agent scope")
            stmt = stmt.where(
                SqlWorkforceConnectorAssignment.scope_kind == kind,
                SqlWorkforceConnectorAssignment.scope_id == sid,
            )
        if enabled is not None:
            stmt = stmt.where(SqlWorkforceConnectorAssignment.enabled == enabled)
        stmt = stmt.order_by(
            SqlWorkforceConnectorAssignment.scope_kind,
            SqlWorkforceConnectorAssignment.scope_id,
            SqlWorkforceConnectorAssignment.connection_id,
            SqlWorkforceConnectorAssignment.service_key,
            SqlWorkforceConnectorAssignment.tool_key,
        )
        with self._session() as session:
            return [_connector(row) for row in session.execute(stmt).scalars().all()]

    def upsert_connector_assignment(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        connection_id: str,
        service_key: str,
        tool_key: str,
        enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceConnectorAssignment:
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise ValueError("connector assignments do not support agent scope")
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceConnectorAssignment).where(
                    SqlWorkforceConnectorAssignment.scope_kind == kind,
                    SqlWorkforceConnectorAssignment.scope_id == sid,
                    SqlWorkforceConnectorAssignment.connection_id == connection_id,
                    SqlWorkforceConnectorAssignment.service_key == service_key,
                    SqlWorkforceConnectorAssignment.tool_key == tool_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceConnectorAssignment(
                    id=_new_id("wfconn"),
                    scope_kind=kind,
                    scope_id=sid,
                    connection_id=connection_id,
                    service_key=service_key,
                    tool_key=tool_key,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.updated_at = now
                row.version += 1
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
            self._bump_revision(session, now)
            session.flush()
            return _connector(row)

    def list_skill_assignments(
        self,
        *,
        scope_kind: str | None = None,
        scope_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[WorkforceSkillAssignment]:
        stmt = select(SqlWorkforceSkillAssignment)
        if scope_kind is not None:
            kind, sid = normalize_scope(scope_kind, scope_id)
            if kind == "agent":
                raise ValueError("skill assignments do not support agent scope")
            stmt = stmt.where(
                SqlWorkforceSkillAssignment.scope_kind == kind,
                SqlWorkforceSkillAssignment.scope_id == sid,
            )
        if enabled is not None:
            stmt = stmt.where(SqlWorkforceSkillAssignment.enabled == enabled)
        stmt = stmt.order_by(
            SqlWorkforceSkillAssignment.scope_kind,
            SqlWorkforceSkillAssignment.scope_id,
            SqlWorkforceSkillAssignment.skill_name,
        )
        with self._session() as session:
            return [_skill(row) for row in session.execute(stmt).scalars().all()]

    def upsert_skill_assignment(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        skill_name: str,
        source: str,
        source_ref: str | None,
        enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceSkillAssignment:
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise ValueError("skill assignments do not support agent scope")
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceSkillAssignment).where(
                    SqlWorkforceSkillAssignment.scope_kind == kind,
                    SqlWorkforceSkillAssignment.scope_id == sid,
                    SqlWorkforceSkillAssignment.skill_name == skill_name,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceSkillAssignment(
                    id=_new_id("wfskill"),
                    scope_kind=kind,
                    scope_id=sid,
                    skill_name=skill_name,
                    source=source,
                    source_ref=source_ref,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.source = source
                row.source_ref = source_ref
                row.enabled = enabled
                row.updated_at = now
                row.version += 1
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
            self._bump_revision(session, now)
            session.flush()
            return _skill(row)

    def list_agent_overrides(
        self,
        *,
        agent_id: str | None = None,
        item_kind: str | None = None,
    ) -> list[WorkforceAgentOverride]:
        stmt = select(SqlWorkforceAgentOverride)
        if agent_id is not None:
            stmt = stmt.where(SqlWorkforceAgentOverride.agent_id == agent_id)
        if item_kind is not None:
            stmt = stmt.where(SqlWorkforceAgentOverride.item_kind == item_kind)
        stmt = stmt.order_by(
            SqlWorkforceAgentOverride.agent_id,
            SqlWorkforceAgentOverride.item_kind,
            SqlWorkforceAgentOverride.item_key,
        )
        with self._session() as session:
            return [_override(row) for row in session.execute(stmt).scalars().all()]

    def upsert_agent_override(
        self,
        *,
        agent_id: str,
        item_kind: str,
        item_key: str,
        enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceAgentOverride:
        if item_kind not in {"connector", "skill"}:
            raise ValueError(f"unsupported override item kind: {item_kind!r}")
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceAgentOverride).where(
                    SqlWorkforceAgentOverride.agent_id == agent_id,
                    SqlWorkforceAgentOverride.item_kind == item_kind,
                    SqlWorkforceAgentOverride.item_key == item_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceAgentOverride(
                    id=_new_id("wfover"),
                    agent_id=agent_id,
                    item_kind=item_kind,
                    item_key=item_key,
                    enabled=enabled,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.updated_at = now
                row.version += 1
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
            self._bump_revision(session, now)
            session.flush()
            return _override(row)

    def list_materializations(
        self,
        *,
        agent_id: str,
        item_kind: str | None = None,
        active: bool | None = None,
    ) -> list[WorkforceAgentMaterialization]:
        stmt = select(SqlWorkforceAgentMaterialization).where(
            SqlWorkforceAgentMaterialization.agent_id == agent_id
        )
        if item_kind is not None:
            stmt = stmt.where(SqlWorkforceAgentMaterialization.item_kind == item_kind)
        if active is not None:
            stmt = stmt.where(SqlWorkforceAgentMaterialization.active == active)
        stmt = stmt.order_by(
            SqlWorkforceAgentMaterialization.item_kind,
            SqlWorkforceAgentMaterialization.item_key,
        )
        with self._session() as session:
            return [_materialization(row) for row in session.execute(stmt).scalars().all()]

    def set_materialization(
        self,
        *,
        agent_id: str,
        item_kind: str,
        item_key: str,
        active: bool,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceAgentMaterialization:
        if item_kind not in {"connector", "skill"}:
            raise ValueError(f"unsupported materialization item kind: {item_kind!r}")
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceAgentMaterialization).where(
                    SqlWorkforceAgentMaterialization.agent_id == agent_id,
                    SqlWorkforceAgentMaterialization.item_kind == item_kind,
                    SqlWorkforceAgentMaterialization.item_key == item_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceAgentMaterialization(
                    id=_new_id("wfmat"),
                    agent_id=agent_id,
                    item_kind=item_kind,
                    item_key=item_key,
                    active=active,
                    created_at=now,
                    updated_at=now,
                    meta=_json_dumps(metadata or {}),
                )
                session.add(row)
            else:
                row.active = active
                row.updated_at = now
                row.meta = _json_dumps(metadata or _json_loads(row.meta, {}))
            session.flush()
            return _materialization(row)


_store_cache: dict[str, SqlAlchemyWorkforceStore] = {}


def get_workforce_store() -> SqlAlchemyWorkforceStore:
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _store_cache.get(location)
    if store is None:
        store = SqlAlchemyWorkforceStore(location)
        _store_cache[location] = store
    return store


def agent_workforce_context(
    agent_id: str,
    *,
    agent_store=None,
    agent_cache=None,
) -> AgentWorkforceContext | None:
    """Resolve agent tier + department from the current agent image."""
    if agent_store is None or agent_cache is None:
        from omnigent.runtime import get_agent_cache, get_agent_store

        agent_store = agent_store or get_agent_store()
        agent_cache = agent_cache or get_agent_cache()
    agent = agent_store.get(agent_id)
    if agent is None:
        return None
    department: str | None = None
    try:
        loaded = agent_cache.load(
            agent.id,
            agent.bundle_location,
            expand_env=agent.session_id is None,
        )
        params = loaded.spec.params if isinstance(loaded.spec.params, dict) else {}
        raw = params.get("department")
        department = str(raw) if raw else None
    except Exception:  # noqa: BLE001 - broken bundles should not break admin lists
        logger.debug("failed to load workforce metadata for agent %s", agent_id, exc_info=True)
    return AgentWorkforceContext(
        agent_id=agent.id,
        agent_name=agent.name,
        category=agent.category,
        bundle_location=agent.bundle_location,
        department=department,
        department_slug=slug(department),
    )


def list_workforce_agent_contexts(
    *,
    agent_store=None,
    agent_cache=None,
) -> list[AgentWorkforceContext]:
    if agent_store is None or agent_cache is None:
        from omnigent.runtime import get_agent_cache, get_agent_store

        agent_store = agent_store or get_agent_store()
        agent_cache = agent_cache or get_agent_cache()
    contexts: list[AgentWorkforceContext] = []
    for agent in agent_store.list(limit=1000, order="asc").data:
        ctx = agent_workforce_context(agent.id, agent_store=agent_store, agent_cache=agent_cache)
        if ctx is not None:
            contexts.append(ctx)
    return contexts


def scopes_for_agent(ctx: AgentWorkforceContext) -> list[tuple[InheritedScopeKind, str]]:
    scopes: list[tuple[InheritedScopeKind, str]] = [("organization", ORG_SCOPE_ID)]
    if ctx.department_slug:
        scopes.append(("department", ctx.department_slug))
    return scopes


def matching_agents_for_scope(
    scope_kind: str,
    scope_id: str | None,
    *,
    agent_store=None,
    agent_cache=None,
) -> list[AgentWorkforceContext]:
    kind, sid = normalize_scope(scope_kind, scope_id)
    if kind == "agent":
        ctx = agent_workforce_context(sid, agent_store=agent_store, agent_cache=agent_cache)
        return [ctx] if ctx is not None else []
    out: list[AgentWorkforceContext] = []
    for ctx in list_workforce_agent_contexts(agent_store=agent_store, agent_cache=agent_cache):
        if not ctx.inheritable:
            continue
        if kind == "organization" or ctx.department_slug == sid:
            out.append(ctx)
    return out


def inherited_instructions_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
) -> list[WorkforceInstruction]:
    store = store or get_workforce_store()
    ctx = agent_workforce_context(agent_id, agent_store=agent_store, agent_cache=agent_cache)
    if ctx is None or not ctx.inheritable:
        return []
    instructions: list[WorkforceInstruction] = []
    for scope_kind, scope_id in scopes_for_agent(ctx):
        instruction = store.get_instruction(scope_kind, scope_id)
        if instruction is not None and instruction.enabled and instruction.body.strip():
            instructions.append(instruction)
    agent_instruction = store.get_instruction("agent", agent_id)
    if (
        agent_instruction is not None
        and agent_instruction.enabled
        and agent_instruction.body.strip()
    ):
        instructions.append(agent_instruction)
    return instructions


_effective_cache: dict[tuple[str, str, int], list[str]] = {}
_effective_cache_lock = threading.RLock()


def clear_effective_cache() -> None:
    with _effective_cache_lock:
        _effective_cache.clear()


def instruction_fragments(
    *,
    agent_id: str | None,
    spec: Any,
) -> list[str]:
    """Extension hook: runtime-composed inherited Work Force instructions."""
    if not agent_id:
        return []
    try:
        store = get_workforce_store()
    except RuntimeError as exc:
        if "runtime not initialized" in str(exc):
            logger.debug("Work Force instructions unavailable before runtime initialization")
            return []
        raise
    revision = store.revision()
    spec_name = str(getattr(spec, "name", "") or "")
    cache_key = (agent_id, spec_name, revision)
    with _effective_cache_lock:
        cached = _effective_cache.get(cache_key)
        if cached is not None:
            return list(cached)
    instructions = inherited_instructions_for_agent(agent_id, store=store)
    fragments: list[str] = []
    for item in instructions:
        label = "Organization"
        if item.scope_kind == "department":
            label = f"Department: {item.scope_id}"
        elif item.scope_kind == "agent":
            label = "Agent"
        fragments.append(f"{label} instructions:\n{item.body.strip()}")
    with _effective_cache_lock:
        _effective_cache[cache_key] = fragments
    return list(fragments)


def inherited_connector_assignments_for_agent(
    ctx: AgentWorkforceContext,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
) -> dict[str, list[WorkforceConnectorAssignment]]:
    store = store or get_workforce_store()
    if not ctx.inheritable:
        return {}
    by_key: dict[str, list[WorkforceConnectorAssignment]] = {}
    for scope_kind, scope_id in scopes_for_agent(ctx):
        for assignment in store.list_connector_assignments(
            scope_kind=scope_kind,
            scope_id=scope_id,
            enabled=True,
        ):
            by_key.setdefault(assignment.item_key, []).append(assignment)
    return by_key


def inherited_skill_assignments_for_agent(
    ctx: AgentWorkforceContext,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
) -> dict[str, list[WorkforceSkillAssignment]]:
    store = store or get_workforce_store()
    if not ctx.inheritable:
        return {}
    by_key: dict[str, list[WorkforceSkillAssignment]] = {}
    for scope_kind, scope_id in scopes_for_agent(ctx):
        for assignment in store.list_skill_assignments(
            scope_kind=scope_kind,
            scope_id=scope_id,
            enabled=True,
        ):
            by_key.setdefault(assignment.item_key, []).append(assignment)
    return by_key


def effective_workforce_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
) -> dict[str, Any]:
    """Return the server-side effective Work Force view for one agent."""
    store = store or get_workforce_store()
    ctx = agent_workforce_context(agent_id, agent_store=agent_store, agent_cache=agent_cache)
    if ctx is None:
        return {"agentId": agent_id, "found": False}
    overrides = {
        (item.item_kind, item.item_key): item
        for item in store.list_agent_overrides(agent_id=agent_id)
    }
    connector_items = []
    for item_key, assignments in inherited_connector_assignments_for_agent(
        ctx, store=store
    ).items():
        override = overrides.get(("connector", item_key))
        enabled = override.enabled if override is not None else True
        first = assignments[-1]
        connector_items.append(
            {
                "itemKey": item_key,
                "connectionId": first.connection_id,
                "serviceKey": first.service_key,
                "toolKey": first.tool_key,
                "enabled": enabled,
                "inherited": True,
                "inheritedFrom": [a.to_dict() for a in assignments],
                "override": override.to_dict() if override else None,
            }
        )
    skill_items = []
    for item_key, assignments in inherited_skill_assignments_for_agent(ctx, store=store).items():
        override = overrides.get(("skill", item_key))
        enabled = override.enabled if override is not None else True
        first = assignments[-1]
        skill_items.append(
            {
                "itemKey": item_key,
                "skillName": first.skill_name,
                "source": first.source,
                "sourceRef": first.source_ref,
                "enabled": enabled,
                "inherited": True,
                "inheritedFrom": [a.to_dict() for a in assignments],
                "override": override.to_dict() if override else None,
            }
        )
    return {
        "agentId": agent_id,
        "found": True,
        "category": ctx.category,
        "department": ctx.department,
        "departmentSlug": ctx.department_slug,
        "revision": store.revision(),
        "instructions": [
            i.to_dict()
            for i in inherited_instructions_for_agent(
                agent_id,
                store=store,
                agent_store=agent_store,
                agent_cache=agent_cache,
            )
        ],
        "connectors": connector_items,
        "skills": skill_items,
        "overrides": [item.to_dict() for item in overrides.values()],
        "materializations": [
            item.to_dict() for item in store.list_materializations(agent_id=agent_id)
        ],
    }


def reconcile_connectors_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    materialize: bool = True,
) -> None:
    """Compile inherited connector assignments to per-agent grants."""
    from bytedesk_omnigent.connectors.grants import materialize_agent_connector_grant
    from bytedesk_omnigent.connectors.store import get_connector_store

    store = store or get_workforce_store()
    ctx = agent_workforce_context(agent_id, agent_store=agent_store, agent_cache=agent_cache)
    if ctx is None or not ctx.inheritable:
        return
    connector_store = get_connector_store()
    inherited = inherited_connector_assignments_for_agent(ctx, store=store)
    overrides = {
        item.item_key: item
        for item in store.list_agent_overrides(agent_id=agent_id, item_kind="connector")
    }
    touched_connections: set[str] = set()

    for item_key, assignments in inherited.items():
        parsed = parse_connector_item_key(item_key)
        if parsed is None:
            continue
        connection_id, service_key, tool_key = parsed
        override = overrides.get(item_key)
        enabled = override.enabled if override is not None else True
        connector_store.upsert_agent_grant(
            connection_id=connection_id,
            agent_id=agent_id,
            service_key=service_key,
            tool_key=tool_key,
            enabled=enabled,
            status="active" if enabled else "disabled",
            metadata={
                "workforceManaged": True,
                "inheritedFrom": [
                    {"scopeKind": a.scope_kind, "scopeId": a.scope_id} for a in assignments
                ],
                "override": override.to_dict() if override is not None else None,
            },
        )
        store.set_materialization(
            agent_id=agent_id,
            item_kind="connector",
            item_key=item_key,
            active=enabled,
            metadata={"workforceManaged": True},
        )
        touched_connections.add(connection_id)

    for override in overrides.values():
        if not override.enabled or override.item_key in inherited:
            continue
        parsed = parse_connector_item_key(override.item_key)
        if parsed is None:
            continue
        connection_id, service_key, tool_key = parsed
        connector_store.upsert_agent_grant(
            connection_id=connection_id,
            agent_id=agent_id,
            service_key=service_key,
            tool_key=tool_key,
            enabled=True,
            status="active",
            metadata={"workforceDirect": True, "override": override.to_dict()},
        )
        touched_connections.add(connection_id)

    desired_keys = set(inherited)
    for grant in connector_store.list_agent_grants(agent_id=agent_id):
        key = connector_item_key(grant.connection_id, grant.service_key, grant.tool_key)
        if not grant.metadata.get("workforceManaged") or key in desired_keys:
            continue
        connector_store.upsert_agent_grant(
            connection_id=grant.connection_id,
            agent_id=agent_id,
            service_key=grant.service_key,
            tool_key=grant.tool_key,
            enabled=False,
            status="disabled",
            metadata={**grant.metadata, "workforceManaged": True, "staleInheritedGrant": True},
        )
        store.set_materialization(
            agent_id=agent_id,
            item_kind="connector",
            item_key=key,
            active=False,
            metadata={"workforceManaged": True, "staleInheritedGrant": True},
        )
        touched_connections.add(grant.connection_id)

    if not materialize:
        return
    for connection_id in touched_connections:
        connection = connector_store.get_connection(connection_id)
        if connection is None:
            continue
        services = connector_store.list_services(connection_id)
        materialize_agent_connector_grant(
            connection=connection,
            services=services,
            grants=connector_store.list_agent_grants(
                connection_id=connection_id,
                agent_id=agent_id,
            ),
            agent_id=agent_id,
            agent_store=agent_store,
            agent_cache=agent_cache,
        )


def disable_connector_grants_for_agent(
    agent_id: str,
    *,
    connector_store=None,
    reason: str = "agent_deleted",
) -> list[str]:
    """Disable active connector grants for one agent id.

    Used when an agent leaves the roster so connector access cannot outlive the
    agent-store source of truth.
    """
    if connector_store is None:
        from bytedesk_omnigent.connectors.store import get_connector_store

        connector_store = get_connector_store()
    disabled: list[str] = []
    for grant in connector_store.list_agent_grants(agent_id=agent_id):
        if not grant.enabled and grant.status == "disabled":
            continue
        connector_store.upsert_agent_grant(
            connection_id=grant.connection_id,
            agent_id=grant.agent_id,
            service_key=grant.service_key,
            tool_key=grant.tool_key,
            enabled=False,
            status="disabled",
            metadata={
                **grant.metadata,
                "staleAgentGrant": True,
                "staleReason": reason,
            },
        )
        disabled.append(grant.agent_id)
    return sorted(set(disabled))


def disable_connector_grants_for_missing_agents(
    *,
    agent_store=None,
    connector_store=None,
) -> list[str]:
    """Disable grants for agent ids no longer present in the template roster."""
    if agent_store is None:
        from omnigent.runtime import get_agent_store

        agent_store = get_agent_store()
    if connector_store is None:
        from bytedesk_omnigent.connectors.store import get_connector_store

        connector_store = get_connector_store()
    active_agent_ids = {agent.id for agent in agent_store.list(limit=1000, order="asc").data}
    disabled: list[str] = []
    for grant in connector_store.list_agent_grants():
        if grant.agent_id in active_agent_ids:
            continue
        if not grant.enabled and grant.status == "disabled":
            continue
        connector_store.upsert_agent_grant(
            connection_id=grant.connection_id,
            agent_id=grant.agent_id,
            service_key=grant.service_key,
            tool_key=grant.tool_key,
            enabled=False,
            status="disabled",
            metadata={
                **grant.metadata,
                "staleAgentGrant": True,
                "staleMissingAgent": True,
            },
        )
        disabled.append(grant.agent_id)
    return sorted(set(disabled))


def _skill_service(agent_store=None, agent_cache=None, artifact_store=None):
    from omnigent.runtime import get_agent_cache, get_agent_store, get_artifact_store
    from omnigent.skills.acquisition import SkillAcquisitionService

    return SkillAcquisitionService(
        agent_store=agent_store or get_agent_store(),
        agent_cache=agent_cache or get_agent_cache(),
        artifact_store=artifact_store or get_artifact_store(),
    )


def _installed_skill_names(service: Any, agent_id: str) -> set[str]:
    return {str(row["name"]) for row in service.installed(agent_id=agent_id)}


def reconcile_skills_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    artifact_store=None,
    service: Any | None = None,
) -> None:
    """Install/remove Work Force-managed inherited skills for one agent."""
    store = store or get_workforce_store()
    ctx = agent_workforce_context(agent_id, agent_store=agent_store, agent_cache=agent_cache)
    if ctx is None or not ctx.inheritable:
        return
    service = service or _skill_service(
        agent_store=agent_store,
        agent_cache=agent_cache,
        artifact_store=artifact_store,
    )
    inherited = inherited_skill_assignments_for_agent(ctx, store=store)
    overrides = {
        item.item_key: item
        for item in store.list_agent_overrides(agent_id=agent_id, item_kind="skill")
    }
    desired: dict[str, WorkforceSkillAssignment] = {}
    for item_key, assignments in inherited.items():
        override = overrides.get(item_key)
        if override is not None and not override.enabled:
            continue
        desired[item_key] = assignments[-1]

    installed = _installed_skill_names(service, agent_id)
    materialized = {
        item.item_key: item
        for item in store.list_materializations(
            agent_id=agent_id,
            item_kind="skill",
            active=True,
        )
    }
    for skill_name, assignment in desired.items():
        if skill_name in installed:
            continue
        preview = service.create_preview(
            operation="install",
            target_agent_ids=[agent_id],
            install_mode="skip_existing",
            source=assignment.source,
            source_ref=assignment.source_ref,
            selected_skill_names=[skill_name],
        )
        results = service.apply_preview(preview.id, agent_ids=[agent_id])
        if any(result.agent_id == agent_id and result.status == "applied" for result in results):
            store.set_materialization(
                agent_id=agent_id,
                item_kind="skill",
                item_key=skill_name,
                active=True,
                metadata={"source": assignment.source, "sourceRef": assignment.source_ref},
            )

    for skill_name in set(materialized) - set(desired):
        if skill_name not in installed:
            store.set_materialization(
                agent_id=agent_id,
                item_kind="skill",
                item_key=skill_name,
                active=False,
                metadata={"reason": "already_absent"},
            )
            continue
        preview = service.create_preview(
            operation="remove",
            target_agent_ids=[agent_id],
            skill_names=[skill_name],
        )
        results = service.apply_preview(preview.id, agent_ids=[agent_id])
        if any(result.agent_id == agent_id and result.status == "applied" for result in results):
            store.set_materialization(
                agent_id=agent_id,
                item_kind="skill",
                item_key=skill_name,
                active=False,
                metadata={"reason": "inherited_disabled"},
            )


def reconcile_workforce_for_agent(agent_id: str, **kwargs: Any) -> None:
    reconcile_connectors_for_agent(agent_id, **kwargs)
    reconcile_skills_for_agent(agent_id, **kwargs)


def reconcile_workforce_for_scope(
    scope_kind: str,
    scope_id: str | None,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    connectors: bool = True,
    skills: bool = True,
    materialize_connectors: bool = True,
) -> list[str]:
    store = store or get_workforce_store()
    agents = matching_agents_for_scope(
        scope_kind,
        scope_id,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )
    reconciled: list[str] = []
    for ctx in agents:
        if connectors:
            reconcile_connectors_for_agent(
                ctx.agent_id,
                store=store,
                agent_store=agent_store,
                agent_cache=agent_cache,
                materialize=materialize_connectors,
            )
        if skills:
            reconcile_skills_for_agent(
                ctx.agent_id,
                store=store,
                agent_store=agent_store,
                agent_cache=agent_cache,
            )
        reconciled.append(ctx.agent_id)
    return reconciled


_agent_bridge_installed = False


def install_workforce_agent_bridge() -> None:
    """Subscribe to agent-store events so new/updated employees inherit config."""
    global _agent_bridge_installed
    if _agent_bridge_installed:
        return
    from omnigent.stores.agent_store import events

    def _listener(event) -> None:
        if event.action not in {"created", "updated", "deleted"}:
            return

        def _run() -> None:
            try:
                if event.action == "deleted":
                    disable_connector_grants_for_agent(event.agent_id)
                    return
                reconcile_workforce_for_agent(event.agent_id)
            except Exception:  # noqa: BLE001 - inheritance bridge is best effort
                logger.warning(
                    "workforce reconciliation failed for agent %s",
                    event.agent_id,
                    exc_info=True,
                )

        threading.Thread(
            target=_run,
            name=f"workforce-reconcile-{event.agent_id}",
            daemon=True,
        ).start()

    _agent_bridge_installed = events.subscribe(_listener)
    if _agent_bridge_installed:

        def _sweep() -> None:
            try:
                disable_connector_grants_for_missing_agents()
            except Exception:  # noqa: BLE001 - cleanup must not block boot
                logger.warning("workforce stale connector grant sweep failed", exc_info=True)

        threading.Thread(
            target=_sweep,
            name="workforce-stale-connector-grant-sweep",
            daemon=True,
        ).start()
