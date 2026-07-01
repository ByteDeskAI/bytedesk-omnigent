"""Work Force inheritance store, resolver, and materialization helpers."""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import select

from bytedesk_omnigent.db_models import (
    SqlWorkforceAgentMaterialization,
    SqlWorkforceAgentOverride,
    SqlWorkforceConnectorAssignment,
    SqlWorkforceInstruction,
    SqlWorkforceRevision,
    SqlWorkforceSkillAssignment,
    SqlWorkforceToolAssignment,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch

logger = logging.getLogger(__name__)

ScopeKind = Literal["organization", "department", "agent"]
InheritedScopeKind = Literal["organization", "department"]
ItemKind = Literal["connector", "skill", "tool"]

ORG_SCOPE_ID = "organization"
REVISION_ID = "workforce"
MANAGED_TOOL_PERMISSIONS_PARAM = "managed_tool_permissions"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_OS_ENV_TOOL_KEYS = frozenset(
    {
        "sys_os_read",
        "sys_os_write",
        "sys_os_edit",
        "sys_os_shell",
    }
)
_TERMINAL_TOOL_KEYS = frozenset(
    {
        "sys_terminal_launch",
        "sys_terminal_send",
        "sys_terminal_read",
        "sys_terminal_list",
        "sys_terminal_close",
    }
)
_TIMER_TOOL_KEYS = frozenset({"sys_timer_set", "sys_timer_cancel"})
_SPAWN_TOOL_KEYS = frozenset(
    {
        "sys_session_create",
        "sys_session_send",
        "sys_session_close",
        "sys_list_models",
    }
)

_STATIC_TOOL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "toolKey": "web_search",
        "label": "Web search",
        "description": "Search the web through the configured model/provider search backend.",
        "group": "Web",
        "mechanism": "builtin",
    },
    {
        "toolKey": "web_fetch",
        "label": "Web fetch",
        "description": "Fetch and summarize a specific URL.",
        "group": "Web",
        "mechanism": "builtin",
    },
    {
        "toolKey": "upload_file",
        "label": "Upload file",
        "description": "Store a file artifact for the active session.",
        "group": "Files",
        "mechanism": "builtin",
    },
    {
        "toolKey": "list_files",
        "label": "List files",
        "description": "List session file artifacts.",
        "group": "Files",
        "mechanism": "builtin",
    },
    {
        "toolKey": "download_file",
        "label": "Download file",
        "description": "Read a stored session file artifact.",
        "group": "Files",
        "mechanism": "builtin",
    },
    {
        "toolKey": "search_conversations",
        "label": "Search conversations",
        "description": "Search available Omnigent conversations.",
        "group": "Knowledge",
        "mechanism": "builtin",
    },
    {
        "toolKey": "export_agent",
        "label": "Export agent",
        "description": "Export an agent image bundle.",
        "group": "Agents",
        "mechanism": "builtin",
    },
    {
        "toolKey": "memory_append",
        "label": "Memory append",
        "description": "Write to the Omnigent agent memory plane.",
        "group": "Memory",
        "mechanism": "builtin",
    },
    {
        "toolKey": "memory_query",
        "label": "Memory query",
        "description": "Query the Omnigent agent memory plane.",
        "group": "Memory",
        "mechanism": "builtin",
    },
    {
        "toolKey": "memory_compartments_list",
        "label": "List memory compartments",
        "description": "List available memory compartments.",
        "group": "Memory",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_search",
        "label": "Search skills",
        "description": "Search available skills.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_sources",
        "label": "Skill sources",
        "description": "List configured skill sources.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_installed",
        "label": "Installed skills",
        "description": "List installed skills on target agents.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_resolve_targets",
        "label": "Resolve skill targets",
        "description": "Resolve skill install/remove target agents.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_stage_preview",
        "label": "Stage skill preview",
        "description": "Create a preview for skill installation or removal.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_apply",
        "label": "Apply skills",
        "description": "Apply a staged skill installation or removal preview.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_skill_remove",
        "label": "Remove skills",
        "description": "Remove skills from target agents.",
        "group": "Skills",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_os_read",
        "label": "Read files",
        "description": "Read from the agent's configured OS environment.",
        "group": "Local OS",
        "mechanism": "os_env",
    },
    {
        "toolKey": "sys_os_write",
        "label": "Write files",
        "description": "Write files in the agent's configured OS environment.",
        "group": "Local OS",
        "mechanism": "os_env",
    },
    {
        "toolKey": "sys_os_edit",
        "label": "Edit files",
        "description": "Patch files in the agent's configured OS environment.",
        "group": "Local OS",
        "mechanism": "os_env",
    },
    {
        "toolKey": "sys_os_shell",
        "label": "Shell commands",
        "description": "Run one-shot shell commands in the agent's configured OS environment.",
        "group": "Local OS",
        "mechanism": "os_env",
    },
    {
        "toolKey": "sys_terminal_launch",
        "label": "Launch bash terminal",
        "description": "Launch an interactive bash terminal.",
        "group": "Terminal",
        "mechanism": "terminal",
    },
    {
        "toolKey": "sys_terminal_send",
        "label": "Send terminal input",
        "description": "Send input to an interactive terminal.",
        "group": "Terminal",
        "mechanism": "terminal",
    },
    {
        "toolKey": "sys_terminal_read",
        "label": "Read terminal output",
        "description": "Read output from an interactive terminal.",
        "group": "Terminal",
        "mechanism": "terminal",
    },
    {
        "toolKey": "sys_terminal_list",
        "label": "List terminals",
        "description": "List active interactive terminals.",
        "group": "Terminal",
        "mechanism": "terminal",
    },
    {
        "toolKey": "sys_terminal_close",
        "label": "Close terminal",
        "description": "Close an interactive terminal.",
        "group": "Terminal",
        "mechanism": "terminal",
    },
    {
        "toolKey": "sys_timer_set",
        "label": "Set timer",
        "description": "Schedule a timer for future agent work.",
        "group": "Scheduling",
        "mechanism": "timer",
    },
    {
        "toolKey": "sys_timer_cancel",
        "label": "Cancel timer",
        "description": "Cancel a scheduled timer.",
        "group": "Scheduling",
        "mechanism": "timer",
    },
    {
        "toolKey": "sys_session_create",
        "label": "Create child session",
        "description": "Spawn a child agent session.",
        "group": "Agents",
        "mechanism": "spawn",
    },
    {
        "toolKey": "sys_session_send",
        "label": "Send to child session",
        "description": "Send work to a child session.",
        "group": "Agents",
        "mechanism": "spawn",
    },
    {
        "toolKey": "sys_session_close",
        "label": "Close child session",
        "description": "Close a child session.",
        "group": "Agents",
        "mechanism": "spawn",
    },
    {
        "toolKey": "sys_list_models",
        "label": "List models",
        "description": "List models available to spawned workers.",
        "group": "Agents",
        "mechanism": "spawn",
    },
    {
        "toolKey": "load_skill",
        "label": "Load skill",
        "description": "Load skill instructions available to the agent.",
        "group": "Skills",
        "mechanism": "managed",
    },
    {
        "toolKey": "read_skill_file",
        "label": "Read skill file",
        "description": "Read bundled resource files from an available skill.",
        "group": "Skills",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_session_list",
        "label": "List sessions",
        "description": "List visible sibling and child sessions.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_session_get_history",
        "label": "Read session history",
        "description": "Read history for a visible session.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_session_get_info",
        "label": "Read session info",
        "description": "Read metadata for a visible session.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_agent_get",
        "label": "Read agent",
        "description": "Read an agent definition.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_agent_download",
        "label": "Download agent",
        "description": "Download an agent image.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_agent_list",
        "label": "List agents",
        "description": "List visible agents.",
        "group": "Agents",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_call_async",
        "label": "Call async tool",
        "description": "Dispatch async work through the tool inbox.",
        "group": "Async",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_read_inbox",
        "label": "Read async inbox",
        "description": "Read completed async work from the inbox.",
        "group": "Async",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_cancel_async",
        "label": "Cancel async work",
        "description": "Cancel async work by handle.",
        "group": "Async",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_cancel_task",
        "label": "Cancel task",
        "description": "Cancel a background task by handle.",
        "group": "Async",
        "mechanism": "managed",
    },
    {
        "toolKey": "list_comments",
        "label": "List comments",
        "description": "Read comments in the active session.",
        "group": "Comments",
        "mechanism": "managed",
    },
    {
        "toolKey": "update_comment",
        "label": "Update comment",
        "description": "Update a comment in the active session.",
        "group": "Comments",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_add_policy",
        "label": "Add policy",
        "description": "Add a runtime policy to the active session.",
        "group": "Policy",
        "mechanism": "managed",
    },
    {
        "toolKey": "sys_policy_registry",
        "label": "Policy registry",
        "description": "List available runtime policy templates.",
        "group": "Policy",
        "mechanism": "managed",
    },
)


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


def workforce_tool_catalog() -> list[dict[str, str]]:
    """Return builtin/runtime tools that Work Force can manage."""
    catalog = [dict(item) for item in _STATIC_TOOL_CATALOG]
    known = {item["toolKey"] for item in catalog}
    try:
        from omnigent.tools.builtins import INSTANTIABLE_BUILTINS

        builtin_names = set(INSTANTIABLE_BUILTINS) | {"web_fetch"}
    except Exception:  # noqa: BLE001 - catalog should not break admin pages
        logger.debug("failed to load builtin tool registry for Work Force catalog", exc_info=True)
        builtin_names = {"web_fetch"}
    for name in sorted(builtin_names - known):
        catalog.append(
            {
                "toolKey": name,
                "label": name.replace("_", " ").title(),
                "description": "Extension-contributed built-in tool.",
                "group": "Built-in",
                "mechanism": "builtin",
            }
        )
    return catalog


def _tool_catalog_by_key() -> dict[str, dict[str, str]]:
    return {item["toolKey"]: item for item in workforce_tool_catalog()}


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
class WorkforceToolAssignment:
    id: str
    scope_kind: InheritedScopeKind
    scope_id: str
    tool_key: str
    enabled: bool
    created_at: int
    updated_at: int
    version: int
    metadata: dict[str, Any]

    @property
    def item_key(self) -> str:
        return self.tool_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopeKind": self.scope_kind,
            "scopeId": self.scope_id,
            "toolKey": self.tool_key,
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


def _tool(row: SqlWorkforceToolAssignment) -> WorkforceToolAssignment:
    return WorkforceToolAssignment(
        id=row.id,
        scope_kind=row.scope_kind,  # type: ignore[arg-type]
        scope_id=row.scope_id,
        tool_key=row.tool_key,
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

    def list_tool_assignments(
        self,
        *,
        scope_kind: str | None = None,
        scope_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[WorkforceToolAssignment]:
        stmt = select(SqlWorkforceToolAssignment)
        if scope_kind is not None:
            kind, sid = normalize_scope(scope_kind, scope_id)
            if kind == "agent":
                raise ValueError("tool assignments do not support agent scope")
            stmt = stmt.where(
                SqlWorkforceToolAssignment.scope_kind == kind,
                SqlWorkforceToolAssignment.scope_id == sid,
            )
        if enabled is not None:
            stmt = stmt.where(SqlWorkforceToolAssignment.enabled == enabled)
        stmt = stmt.order_by(
            SqlWorkforceToolAssignment.scope_kind,
            SqlWorkforceToolAssignment.scope_id,
            SqlWorkforceToolAssignment.tool_key,
        )
        with self._session() as session:
            return [_tool(row) for row in session.execute(stmt).scalars().all()]

    def upsert_tool_assignment(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        tool_key: str,
        enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> WorkforceToolAssignment:
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise ValueError("tool assignments do not support agent scope")
        if tool_key not in _tool_catalog_by_key():
            raise ValueError(f"unsupported workforce tool: {tool_key!r}")
        now = now_epoch()
        with self._write_session() as session:
            row = session.execute(
                select(SqlWorkforceToolAssignment).where(
                    SqlWorkforceToolAssignment.scope_kind == kind,
                    SqlWorkforceToolAssignment.scope_id == sid,
                    SqlWorkforceToolAssignment.tool_key == tool_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlWorkforceToolAssignment(
                    id=_new_id("wftool"),
                    scope_kind=kind,
                    scope_id=sid,
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
            return _tool(row)

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
        if item_kind not in {"connector", "skill", "tool"}:
            raise ValueError(f"unsupported override item kind: {item_kind!r}")
        if item_kind == "tool" and item_key not in _tool_catalog_by_key():
            raise ValueError(f"unsupported workforce tool: {item_key!r}")
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
        if item_kind not in {"connector", "skill", "tool"}:
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


def inherited_tool_assignments_for_agent(
    ctx: AgentWorkforceContext,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
) -> dict[str, list[WorkforceToolAssignment]]:
    store = store or get_workforce_store()
    if not ctx.inheritable:
        return {}
    by_key: dict[str, list[WorkforceToolAssignment]] = {}
    for scope_kind, scope_id in scopes_for_agent(ctx):
        for assignment in store.list_tool_assignments(
            scope_kind=scope_kind,
            scope_id=scope_id,
        ):
            by_key.setdefault(assignment.item_key, []).append(assignment)
    return by_key


def _effective_tool_items(
    ctx: AgentWorkforceContext,
    *,
    store: SqlAlchemyWorkforceStore,
    overrides: dict[tuple[str, str], WorkforceAgentOverride],
) -> list[dict[str, Any]]:
    catalog = _tool_catalog_by_key()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item_key, assignments in inherited_tool_assignments_for_agent(ctx, store=store).items():
        override = overrides.get(("tool", item_key))
        inherited_enabled = assignments[-1].enabled
        enabled = override.enabled if override is not None else inherited_enabled
        catalog_item = catalog.get(item_key, {})
        items.append(
            {
                "itemKey": item_key,
                "toolKey": item_key,
                "label": catalog_item.get("label", item_key),
                "description": catalog_item.get("description", ""),
                "group": catalog_item.get("group", "Built-in"),
                "mechanism": catalog_item.get("mechanism", "managed"),
                "enabled": enabled,
                "inherited": True,
                "inheritedFrom": [a.to_dict() for a in assignments],
                "override": override.to_dict() if override else None,
            }
        )
        seen.add(item_key)
    for override in overrides.values():
        if override.item_kind != "tool" or override.item_key in seen:
            continue
        catalog_item = catalog.get(override.item_key, {})
        items.append(
            {
                "itemKey": override.item_key,
                "toolKey": override.item_key,
                "label": catalog_item.get("label", override.item_key),
                "description": catalog_item.get("description", ""),
                "group": catalog_item.get("group", "Built-in"),
                "mechanism": catalog_item.get("mechanism", "managed"),
                "enabled": override.enabled,
                "inherited": False,
                "inheritedFrom": [],
                "override": override.to_dict(),
            }
        )
    return sorted(items, key=lambda item: (str(item["group"]).lower(), str(item["label"]).lower()))


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
    tool_items = _effective_tool_items(ctx, store=store, overrides=overrides)
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
        "tools": tool_items,
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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _builtin_entry_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return str(entry["name"])
    return None


def _default_os_env_config() -> dict[str, Any]:
    return {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {"type": "none"},
    }


def _ensure_os_env_config(config: dict[str, Any]) -> None:
    if isinstance(config.get("os_env"), dict):
        return
    config["os_env"] = _default_os_env_config()


def _ensure_bash_terminal_config(config: dict[str, Any]) -> None:
    _ensure_os_env_config(config)
    terminals = _mapping(config.get("terminals"))
    terminals.setdefault(
        "bash",
        {
            "command": "bash",
            "args": ["-l"],
            "os_env": "inherit",
            "allow_cwd_override": True,
            "allow_sandbox_override": False,
            "scrollback": 10000,
        },
    )
    config["terminals"] = terminals


def _tool_keys_by_mechanism(tool_keys: set[str], mechanism: str) -> set[str]:
    catalog = _tool_catalog_by_key()
    return {
        key
        for key in tool_keys
        if catalog.get(key, {}).get("mechanism") == mechanism
        or (mechanism == "builtin" and key not in catalog)
    }


def _materialize_tool_config(
    config: dict[str, Any],
    *,
    managed_keys: set[str],
    enabled_keys: set[str],
) -> dict[str, Any]:
    next_config = copy.deepcopy(config)
    tools = _mapping(next_config.get("tools"))
    existing_builtins = (
        list(tools.get("builtins")) if isinstance(tools.get("builtins"), list) else []
    )
    builtin_managed = _tool_keys_by_mechanism(managed_keys, "builtin")
    builtin_enabled = _tool_keys_by_mechanism(enabled_keys, "builtin")
    builtins = [
        entry
        for entry in existing_builtins
        if not (
            (name := _builtin_entry_name(entry)) is not None
            and name in builtin_managed
            and name not in builtin_enabled
        )
    ]
    present = {name for entry in builtins if (name := _builtin_entry_name(entry)) is not None}
    for key in sorted(builtin_enabled - present):
        builtins.append(key)
    if builtins or "builtins" in tools:
        tools["builtins"] = builtins
    if tools:
        next_config["tools"] = tools

    if enabled_keys & _OS_ENV_TOOL_KEYS:
        _ensure_os_env_config(next_config)
    if enabled_keys & _TERMINAL_TOOL_KEYS:
        _ensure_bash_terminal_config(next_config)
    if enabled_keys & _TIMER_TOOL_KEYS:
        next_config["timers"] = True
    if enabled_keys & _SPAWN_TOOL_KEYS:
        next_config["spawn"] = True

    params = _mapping(next_config.get("params"))
    if managed_keys:
        params[MANAGED_TOOL_PERMISSIONS_PARAM] = {
            "managed": sorted(managed_keys),
            "enabled": sorted(enabled_keys & managed_keys),
        }
    else:
        params.pop(MANAGED_TOOL_PERMISSIONS_PARAM, None)
    if params:
        next_config["params"] = params
    elif "params" in next_config:
        del next_config["params"]
    return next_config


def _load_config_from_workdir(workdir: Path) -> dict[str, Any]:
    path = workdir / "config.yaml"
    loaded = yaml.safe_load(path.read_text()) if path.is_file() else {}
    return loaded if isinstance(loaded, dict) else {}


def _bundle_with_config(src_workdir: Path, config: dict[str, Any]) -> bytes:
    from omnigent.spec.tar_utils import build_bundle_bytes

    staging = Path(tempfile.mkdtemp(prefix="workforce_tools_"))
    try:
        shutil.copytree(src_workdir, staging, dirs_exist_ok=True)
        (staging / ".omnigent-bundle-location").unlink(missing_ok=True)
        (staging / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        return build_bundle_bytes(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def reconcile_tools_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    artifact_store=None,
) -> None:
    """Compile inherited builtin-tool permissions into an agent image."""
    store = store or get_workforce_store()
    if agent_store is None or agent_cache is None or artifact_store is None:
        from omnigent.runtime import get_agent_cache, get_agent_store, get_artifact_store

        agent_store = agent_store or get_agent_store()
        agent_cache = agent_cache or get_agent_cache()
        artifact_store = artifact_store or get_artifact_store()
    ctx = agent_workforce_context(agent_id, agent_store=agent_store, agent_cache=agent_cache)
    if ctx is None or not ctx.inheritable:
        return
    agent = agent_store.get(agent_id)
    if agent is None or agent.session_id is not None:
        return
    overrides = {
        (item.item_kind, item.item_key): item
        for item in store.list_agent_overrides(agent_id=agent_id)
    }
    tool_items = _effective_tool_items(ctx, store=store, overrides=overrides)
    managed_keys = {str(item["toolKey"]) for item in tool_items}
    enabled_keys = {str(item["toolKey"]) for item in tool_items if item["enabled"]}

    for item in tool_items:
        tool_key = str(item["toolKey"])
        store.set_materialization(
            agent_id=agent_id,
            item_kind="tool",
            item_key=tool_key,
            active=tool_key in enabled_keys,
            metadata={
                "workforceManaged": True,
                "inherited": bool(item["inherited"]),
                "mechanism": str(item["mechanism"]),
            },
        )
    for materialization in store.list_materializations(agent_id=agent_id, item_kind="tool"):
        if materialization.item_key in managed_keys or not materialization.active:
            continue
        store.set_materialization(
            agent_id=agent_id,
            item_kind="tool",
            item_key=materialization.item_key,
            active=False,
            metadata={"workforceManaged": True, "staleToolPermission": True},
        )

    loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=False)
    current_config = _load_config_from_workdir(loaded.workdir)
    next_config = _materialize_tool_config(
        current_config,
        managed_keys=managed_keys,
        enabled_keys=enabled_keys,
    )
    if next_config == current_config:
        return

    from omnigent.server.agent_write import apply_bundle_update
    from omnigent.server.auth import local_single_user_enabled
    from omnigent.server.bundles import validate_agent_bundle

    bundle_bytes = _bundle_with_config(loaded.workdir, next_config)
    spec = validate_agent_bundle(
        bundle_bytes,
        enforce_handler_allowlist=not local_single_user_enabled(),
    )
    if spec.name is not None and spec.name != agent.name:
        raise RuntimeError(
            "Work Force tool materialization changed spec name "
            f"from {agent.name!r} to {spec.name!r}"
        )
    updated = apply_bundle_update(
        agent,
        bundle_bytes,
        artifact_store=artifact_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        expand_env=True,
    )
    try:
        agent_store.set_sot_tier(updated.id, "migrated")
        agent_store.set_capabilities(updated.id, spec.capabilities)
    except AttributeError:
        pass


def reconcile_workforce_for_agent(
    agent_id: str,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    artifact_store=None,
    service=None,
    connectors: bool = True,
    skills: bool = True,
    tools: bool = True,
    materialize_connectors: bool = True,
) -> None:
    if connectors:
        reconcile_connectors_for_agent(
            agent_id,
            store=store,
            agent_store=agent_store,
            agent_cache=agent_cache,
            materialize=materialize_connectors,
        )
    if skills:
        reconcile_skills_for_agent(
            agent_id,
            store=store,
            agent_store=agent_store,
            agent_cache=agent_cache,
            artifact_store=artifact_store,
            service=service,
        )
    if tools:
        reconcile_tools_for_agent(
            agent_id,
            store=store,
            agent_store=agent_store,
            agent_cache=agent_cache,
            artifact_store=artifact_store,
        )


def reconcile_workforce_for_scope(
    scope_kind: str,
    scope_id: str | None,
    *,
    store: SqlAlchemyWorkforceStore | None = None,
    agent_store=None,
    agent_cache=None,
    artifact_store=None,
    connectors: bool = True,
    skills: bool = True,
    tools: bool = True,
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
        if tools:
            reconcile_tools_for_agent(
                ctx.agent_id,
                store=store,
                agent_store=agent_store,
                agent_cache=agent_cache,
                artifact_store=artifact_store,
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
