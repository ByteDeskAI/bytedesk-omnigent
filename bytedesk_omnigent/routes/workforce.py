"""Admin API for Work Force inheritance."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from bytedesk_omnigent.connectors.store import get_connector_store
from bytedesk_omnigent.routes.connectors import AgentGrantBody, _grant_targets
from bytedesk_omnigent.workforce import (
    ORG_SCOPE_ID,
    effective_workforce_for_agent,
    get_workforce_store,
    list_workforce_agent_contexts,
    matching_agents_for_scope,
    normalize_scope,
    reconcile_connectors_for_agent,
    reconcile_skills_for_agent,
    reconcile_tools_for_agent,
    reconcile_workforce_for_scope,
    workforce_tool_catalog,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.stores.permission_store import PermissionStore


class InstructionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    body: str = Field(default="", validation_alias=AliasChoices("body", "instructions"))
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorAssignmentBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connection_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("connectionId", "connection_id"),
    )
    services: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    service_tools: dict[str, list[str]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("serviceTools", "service_tools"),
    )
    enabled: bool = True
    replace: bool = True
    reconcile: bool = True
    materialize: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillAssignmentBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    skill_name: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("skillName", "skill_name"),
    )
    source: str = Field(default="skills", max_length=64)
    source_ref: str | None = Field(
        default=None,
        max_length=512,
        validation_alias=AliasChoices("sourceRef", "source_ref"),
    )
    enabled: bool = True
    reconcile: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolAssignmentBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tool_key: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("toolKey", "tool_key"),
    )
    enabled: bool = True
    reconcile: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentOverrideBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    item_kind: str = Field(validation_alias=AliasChoices("itemKind", "item_kind"))
    item_key: str = Field(validation_alias=AliasChoices("itemKey", "item_key"))
    enabled: bool
    reconcile: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    from omnigent.server.routes._auth_helpers import get_user_id

    user_id = get_user_id(request, auth_provider)
    if permission_store is None:
        return
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    import asyncio

    if not await asyncio.to_thread(permission_store.is_admin, user_id):
        raise OmnigentError(
            "Admin privileges required to manage Work Force",
            code=ErrorCode.FORBIDDEN,
        )


def _scope_response(scope_kind: str, scope_id: str | None) -> dict[str, Any]:
    kind, sid = normalize_scope(scope_kind, scope_id)
    store = get_workforce_store()
    return {
        "scopeKind": kind,
        "scopeId": sid,
        "instruction": (
            instruction.to_dict()
            if (instruction := store.get_instruction(kind, sid)) is not None
            else None
        ),
        "connectors": [
            item.to_dict()
            for item in store.list_connector_assignments(scope_kind=kind, scope_id=sid)
        ],
        "skills": [
            item.to_dict() for item in store.list_skill_assignments(scope_kind=kind, scope_id=sid)
        ],
        "tools": [
            item.to_dict() for item in store.list_tool_assignments(scope_kind=kind, scope_id=sid)
        ],
        "revision": store.revision(),
    }


def create_workforce_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/workforce/scopes")
    async def list_scopes(request: Request) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        departments: dict[str, dict[str, Any]] = {}
        for ctx in list_workforce_agent_contexts():
            if not ctx.inheritable or ctx.department_slug is None:
                continue
            row = departments.setdefault(
                ctx.department_slug,
                {
                    "scopeKind": "department",
                    "scopeId": ctx.department_slug,
                    "label": ctx.department or ctx.department_slug,
                    "agentIds": [],
                },
            )
            row["agentIds"].append(ctx.agent_id)
        department_rows = sorted(
            departments.values(),
            key=lambda item: str(item["label"]).lower(),
        )
        return JSONResponse(
            {
                "scopes": [
                    {
                        "scopeKind": "organization",
                        "scopeId": ORG_SCOPE_ID,
                        "label": "Organization",
                        "agentIds": [
                            ctx.agent_id for ctx in matching_agents_for_scope("organization", None)
                        ],
                    },
                    *department_rows,
                ],
                "revision": get_workforce_store().revision(),
            }
        )

    @router.get("/workforce/tools/catalog")
    async def get_tool_catalog(request: Request) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        return JSONResponse({"tools": workforce_tool_catalog()})

    @router.get("/workforce/scopes/{scope_kind}/{scope_id}")
    async def get_scope(
        request: Request,
        scope_kind: str,
        scope_id: str,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        return JSONResponse(_scope_response(scope_kind, scope_id))

    @router.get("/workforce/scopes/organization")
    async def get_organization_scope(request: Request) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        return JSONResponse(_scope_response("organization", None))

    @router.put("/workforce/scopes/{scope_kind}/{scope_id}/instructions")
    async def set_scope_instructions(
        request: Request,
        scope_kind: str,
        scope_id: str,
        body: InstructionBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        instruction = get_workforce_store().set_instruction(
            scope_kind=scope_kind,
            scope_id=scope_id,
            body=body.body,
            enabled=body.enabled,
            metadata=body.metadata,
        )
        return JSONResponse(
            {
                "instruction": instruction.to_dict(),
                "scope": _scope_response(scope_kind, scope_id),
            }
        )

    @router.put("/workforce/scopes/organization/instructions")
    async def set_organization_instructions(
        request: Request,
        body: InstructionBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        instruction = get_workforce_store().set_instruction(
            scope_kind="organization",
            scope_id=None,
            body=body.body,
            enabled=body.enabled,
            metadata=body.metadata,
        )
        return JSONResponse(
            {
                "instruction": instruction.to_dict(),
                "scope": _scope_response("organization", None),
            }
        )

    @router.post("/workforce/scopes/{scope_kind}/{scope_id}/connectors")
    async def upsert_scope_connector(
        request: Request,
        scope_kind: str,
        scope_id: str,
        body: ConnectorAssignmentBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise OmnigentError(
                "agent connector overrides use /workforce/agents",
                code=ErrorCode.INVALID_INPUT,
            )
        connector_store = get_connector_store()
        connection = connector_store.get_connection(body.connection_id)
        if connection is None:
            raise OmnigentError("connector connection not found", code=ErrorCode.NOT_FOUND)
        from bytedesk_omnigent.connectors.registry import build_connector_registry

        services = {svc.service_key: svc for svc in connector_store.list_services(connection.id)}
        requested = _grant_targets(
            registry=build_connector_registry(),
            connection=connection,
            known_services=services,
            body=AgentGrantBody(
                agentId="scope",
                services=body.services,
                tools=body.tools,
                serviceTools=body.service_tools,
                enabled=body.enabled,
                replace=body.replace,
                materialize=body.materialize,
            ),
        )
        requested_set = set(requested)
        store = get_workforce_store()
        assignments = [
            store.upsert_connector_assignment(
                scope_kind=kind,
                scope_id=sid,
                connection_id=body.connection_id,
                service_key=service_key,
                tool_key=tool_key,
                enabled=body.enabled,
                metadata=body.metadata,
            )
            for service_key, tool_key in requested
        ]
        if body.enabled and body.replace:
            for existing in store.list_connector_assignments(scope_kind=kind, scope_id=sid):
                if existing.connection_id != body.connection_id:
                    continue
                if (existing.service_key, existing.tool_key) in requested_set:
                    continue
                store.upsert_connector_assignment(
                    scope_kind=kind,
                    scope_id=sid,
                    connection_id=existing.connection_id,
                    service_key=existing.service_key,
                    tool_key=existing.tool_key,
                    enabled=False,
                    metadata=existing.metadata,
                )
        reconciled: list[str] = []
        if body.reconcile:
            reconciled = reconcile_workforce_for_scope(
                kind,
                sid,
                store=store,
                connectors=True,
                skills=False,
                tools=False,
                materialize_connectors=body.materialize,
            )
        return JSONResponse(
            {
                "assignments": [item.to_dict() for item in assignments],
                "reconciledAgentIds": reconciled,
                "scope": _scope_response(kind, sid),
            }
        )

    @router.post("/workforce/scopes/organization/connectors")
    async def upsert_organization_connector(
        request: Request,
        body: ConnectorAssignmentBody,
    ) -> JSONResponse:
        return await upsert_scope_connector(request, "organization", ORG_SCOPE_ID, body)

    @router.post("/workforce/scopes/{scope_kind}/{scope_id}/skills")
    async def upsert_scope_skill(
        request: Request,
        scope_kind: str,
        scope_id: str,
        body: SkillAssignmentBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise OmnigentError(
                "agent skill overrides use /workforce/agents",
                code=ErrorCode.INVALID_INPUT,
            )
        store = get_workforce_store()
        assignment = store.upsert_skill_assignment(
            scope_kind=kind,
            scope_id=sid,
            skill_name=body.skill_name,
            source=body.source,
            source_ref=body.source_ref,
            enabled=body.enabled,
            metadata=body.metadata,
        )
        reconciled: list[str] = []
        if body.reconcile:
            reconciled = reconcile_workforce_for_scope(
                kind,
                sid,
                store=store,
                connectors=False,
                skills=True,
                tools=False,
            )
        return JSONResponse(
            {
                "assignment": assignment.to_dict(),
                "reconciledAgentIds": reconciled,
                "scope": _scope_response(kind, sid),
            }
        )

    @router.post("/workforce/scopes/organization/skills")
    async def upsert_organization_skill(
        request: Request,
        body: SkillAssignmentBody,
    ) -> JSONResponse:
        return await upsert_scope_skill(request, "organization", ORG_SCOPE_ID, body)

    @router.post("/workforce/scopes/{scope_kind}/{scope_id}/tools")
    async def upsert_scope_tool(
        request: Request,
        scope_kind: str,
        scope_id: str,
        body: ToolAssignmentBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        kind, sid = normalize_scope(scope_kind, scope_id)
        if kind == "agent":
            raise OmnigentError(
                "agent tool overrides use /workforce/agents",
                code=ErrorCode.INVALID_INPUT,
            )
        store = get_workforce_store()
        try:
            assignment = store.upsert_tool_assignment(
                scope_kind=kind,
                scope_id=sid,
                tool_key=body.tool_key,
                enabled=body.enabled,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        reconciled: list[str] = []
        if body.reconcile:
            reconciled = reconcile_workforce_for_scope(
                kind,
                sid,
                store=store,
                connectors=False,
                skills=False,
                tools=True,
            )
        return JSONResponse(
            {
                "assignment": assignment.to_dict(),
                "reconciledAgentIds": reconciled,
                "scope": _scope_response(kind, sid),
            }
        )

    @router.post("/workforce/scopes/organization/tools")
    async def upsert_organization_tool(
        request: Request,
        body: ToolAssignmentBody,
    ) -> JSONResponse:
        return await upsert_scope_tool(request, "organization", ORG_SCOPE_ID, body)

    @router.get("/workforce/agents/{agent_id}/effective")
    async def get_agent_effective(
        request: Request,
        agent_id: str,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        effective = effective_workforce_for_agent(agent_id)
        if effective.get("found") is False:
            raise OmnigentError("agent not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse(effective)

    @router.put("/workforce/agents/{agent_id}/instructions")
    async def set_agent_instructions(
        request: Request,
        agent_id: str,
        body: InstructionBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        effective = effective_workforce_for_agent(agent_id)
        if effective.get("found") is False:
            raise OmnigentError("agent not found", code=ErrorCode.NOT_FOUND)
        store = get_workforce_store()
        instruction = store.set_instruction(
            scope_kind="agent",
            scope_id=agent_id,
            body=body.body,
            enabled=body.enabled,
            metadata=body.metadata,
        )
        return JSONResponse(
            {
                "instruction": instruction.to_dict(),
                "effective": effective_workforce_for_agent(agent_id, store=store),
            }
        )

    @router.post("/workforce/agents/{agent_id}/overrides")
    async def upsert_agent_override(
        request: Request,
        agent_id: str,
        body: AgentOverrideBody,
    ) -> JSONResponse:
        await _require_admin(request, auth_provider, permission_store)
        if body.item_kind not in {"connector", "skill", "tool"}:
            raise OmnigentError("unsupported override item kind", code=ErrorCode.INVALID_INPUT)
        store = get_workforce_store()
        try:
            override = store.upsert_agent_override(
                agent_id=agent_id,
                item_kind=body.item_kind,
                item_key=body.item_key,
                enabled=body.enabled,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if body.reconcile:
            if body.item_kind == "connector":
                reconcile_connectors_for_agent(agent_id, store=store)
            elif body.item_kind == "skill":
                reconcile_skills_for_agent(agent_id, store=store)
            else:
                reconcile_tools_for_agent(agent_id, store=store)
        return JSONResponse(
            {
                "override": override.to_dict(),
                "effective": effective_workforce_for_agent(agent_id, store=store),
            }
        )

    return router


__all__ = ["create_workforce_router"]
