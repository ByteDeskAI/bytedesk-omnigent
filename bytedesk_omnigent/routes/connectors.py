"""Admin API for Omnigent connectors."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from bytedesk_omnigent.connectors.credentials import load_connector_secret
from bytedesk_omnigent.connectors.grants import materialize_agent_connector_grant
from bytedesk_omnigent.connectors.providers import (
    ConnectorCreateRequest,
    ConnectorOAuthCallbackRequest,
    ConnectorOAuthStartRequest,
)
from bytedesk_omnigent.connectors.registry import ConnectorRegistry, build_connector_registry
from bytedesk_omnigent.connectors.store import ConnectorConnection, get_connector_store
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.agent_refs import resolve_agent_ref
from omnigent.server.auth import AuthProvider
from omnigent.stores import AgentStore
from omnigent.stores.permission_store import PermissionStore


class ConnectorOAuthStartBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    redirect_uri: str | None = Field(
        default=None,
        validation_alias=AliasChoices("redirectUri", "redirect_uri"),
    )


class ConnectorConnectionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("displayName", "display_name"),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        default=None,
        max_length=256,
        validation_alias=AliasChoices("secretRef", "secret_ref"),
    )
    secret_payload: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("secretPayload", "secret_payload"),
    )
    enabled_services: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("enabledServices", "enabled_services"),
    )


class ServiceToggleBody(BaseModel):
    enabled: bool


class AgentGrantBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("agentId", "agent_id"),
    )
    services: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    service_tools: dict[str, list[str]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("serviceTools", "service_tools"),
    )
    enabled: bool = True
    replace: bool = True
    materialize: bool = True


def _provider_key(provider: str) -> str:
    return provider.replace("-", "_")


def _connection_with_services(
    connection: ConnectorConnection,
    *,
    store=None,
) -> dict[str, Any]:
    store = store or get_connector_store()
    return {
        **connection.to_dict(),
        "services": [svc.to_dict() for svc in store.list_services(connection.id)],
        "grants": [
            grant.to_dict() for grant in store.list_agent_grants(connection_id=connection.id)
        ],
    }


def _require_provider(registry: ConnectorRegistry, provider: str):
    adapter = registry.get_provider(_provider_key(provider))
    if adapter is None:
        raise OmnigentError("connector provider not found", code=ErrorCode.NOT_FOUND)
    return adapter


def _parse_tool_token(token: str) -> tuple[str, str]:
    if ":" in token:
        service_key, tool_key = token.split(":", 1)
    elif "/" in token:
        service_key, tool_key = token.split("/", 1)
    else:
        raise OmnigentError(
            f"connector tool must be service:tool, got {token!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    service_key = service_key.strip()
    tool_key = tool_key.strip()
    if not service_key or not tool_key:
        raise OmnigentError(
            f"connector tool must be service:tool, got {token!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return service_key, tool_key


def _grant_targets(
    *,
    registry: ConnectorRegistry,
    connection: ConnectorConnection,
    known_services: dict[str, Any],
    body: AgentGrantBody,
) -> list[tuple[str, str]]:
    manifest = registry.get(connection.provider)
    if manifest is None:
        raise OmnigentError("connector provider not found", code=ErrorCode.NOT_FOUND)
    tools_by_service = {svc.key: [tool.key for tool in svc.tools] for svc in manifest.services}
    selected: list[tuple[str, str]] = []

    if body.tools:
        selected.extend(_parse_tool_token(token) for token in body.tools)
    for service_key, tool_keys in body.service_tools.items():
        selected.extend((service_key, tool_key) for tool_key in tool_keys)
    if not selected:
        service_keys = body.services or [key for key, svc in known_services.items() if svc.enabled]
        for service_key in service_keys:
            selected.extend(
                (service_key, tool_key) for tool_key in tools_by_service.get(service_key, [])
            )

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for service_key, tool_key in selected:
        if service_key not in known_services:
            raise OmnigentError(
                f"unknown connector service: {service_key}",
                code=ErrorCode.INVALID_INPUT,
            )
        if tool_key not in tools_by_service.get(service_key, []):
            raise OmnigentError(
                f"unknown connector tool: {service_key}:{tool_key}",
                code=ErrorCode.INVALID_INPUT,
            )
        target = (service_key, tool_key)
        if target not in seen:
            deduped.append(target)
            seen.add(target)
    return deduped


def create_connectors_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_store: AgentStore | None = None,
) -> APIRouter:
    router = APIRouter()

    def _normalize_agent_ref(agent_ref: str, *, missing_ok: bool = False) -> str | None:
        ref = agent_ref.strip()
        if not ref:
            return None if missing_ok else agent_ref
        try:
            store = agent_store
            if store is None:
                from omnigent.runtime import get_agent_store

                store = get_agent_store()
            agent = resolve_agent_ref(store, ref, template_only=True)
        except Exception as exc:
            if ref.startswith("ag_"):
                return ref
            if missing_ok:
                return None
            raise OmnigentError(
                f"Agent not found or not bindable: {agent_ref!r}",
                code=ErrorCode.NOT_FOUND,
            ) from exc
        if agent is not None:
            return agent.id
        if ref.startswith("ag_"):
            return ref
        if missing_ok:
            return None
        raise OmnigentError(
            f"Agent not found or not bindable: {agent_ref!r}",
            code=ErrorCode.NOT_FOUND,
        )

    async def _require_admin(request: Request) -> None:
        from omnigent.server.routes._auth_helpers import get_user_id

        user_id = get_user_id(request, auth_provider)
        if permission_store is None:
            return
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        import asyncio

        if not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required to manage connectors",
                code=ErrorCode.FORBIDDEN,
            )

    @router.get("/connectors/catalog")
    async def catalog(request: Request) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        connections = store.list_connections()
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for conn in connections:
            by_provider.setdefault(conn.provider, []).append(
                _connection_with_services(conn, store=store)
            )
        return JSONResponse(
            {
                "providers": [
                    {
                        **manifest.to_dict(),
                        "connections": by_provider.get(manifest.provider, []),
                    }
                    for manifest in build_connector_registry().providers()
                ]
            }
        )

    @router.get("/connectors/connections")
    async def list_connections(
        request: Request,
        provider: str | None = Query(default=None),
    ) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        normalized_provider = _provider_key(provider) if provider else None
        return JSONResponse(
            {
                "connections": [
                    _connection_with_services(conn, store=store)
                    for conn in store.list_connections(provider=normalized_provider)
                ]
            }
        )

    @router.get("/connectors/connections/{connection_id}")
    async def get_connection(request: Request, connection_id: str) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        conn = store.get_connection(connection_id)
        if conn is None:
            raise OmnigentError("connector connection not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"connection": _connection_with_services(conn, store=store)})

    @router.post("/connectors/{provider}/oauth/start")
    async def oauth_start(
        request: Request,
        provider: str,
        body: ConnectorOAuthStartBody | None = None,
    ) -> JSONResponse:
        await _require_admin(request)
        adapter = _require_provider(build_connector_registry(), provider)
        result = await adapter.start_oauth(
            ConnectorOAuthStartRequest(redirect_uri=body.redirect_uri if body else None),
            store=get_connector_store(),
        )
        return JSONResponse(
            {
                "authorizationUrl": result.authorization_url,
                "state": result.state,
            }
        )

    @router.get("/connectors/{provider}/oauth/callback", response_model=None)
    async def oauth_callback(
        request: Request,
        provider: str,
        code: Annotated[str, Query(min_length=1)],
        state: Annotated[str, Query(min_length=1)],
        redirect: bool = Query(default=False),
    ) -> JSONResponse | RedirectResponse:
        await _require_admin(request)
        store = get_connector_store()
        adapter = _require_provider(build_connector_registry(), provider)
        connection = await adapter.complete_oauth(
            ConnectorOAuthCallbackRequest(code=code, state=state),
            store=store,
        )
        if redirect:
            return RedirectResponse(url="/connectors")
        return JSONResponse(
            {"connection": _connection_with_services(connection, store=store)},
            status_code=201,
        )

    @router.post("/connectors/{provider}/connections")
    async def create_connection(
        request: Request,
        provider: str,
        body: ConnectorConnectionBody,
    ) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        adapter = _require_provider(build_connector_registry(), provider)
        connection = adapter.create_connection(
            ConnectorCreateRequest(
                display_name=body.display_name,
                metadata=body.metadata,
                secret_ref=body.secret_ref,
                secret_payload=body.secret_payload,
                enabled_services=body.enabled_services,
            ),
            store=store,
        )
        return JSONResponse(
            {"connection": _connection_with_services(connection, store=store)},
            status_code=201,
        )

    @router.patch("/connectors/connections/{connection_id}/services/{service_key}")
    async def toggle_service(
        request: Request,
        connection_id: str,
        service_key: str,
        body: ServiceToggleBody,
    ) -> JSONResponse:
        await _require_admin(request)
        svc = get_connector_store().set_service_enabled(connection_id, service_key, body.enabled)
        if svc is None:
            raise OmnigentError("connector service not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"service": svc.to_dict()})

    @router.post("/connectors/connections/{connection_id}/health-check")
    async def health_check(
        request: Request,
        connection_id: str,
        live: bool = Query(default=False),
    ) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        conn = store.get_connection(connection_id)
        if conn is None:
            raise OmnigentError("connector connection not found", code=ErrorCode.NOT_FOUND)
        adapter = _require_provider(build_connector_registry(), conn.provider)
        secret_payload = load_connector_secret(conn.secret_ref)
        result = (
            adapter.check_live_health(conn, secret_payload)
            if live
            else adapter.check_health(conn, secret_payload)
        )
        updated = store.update_health(
            connection_id,
            status=result.status,
            error=result.error,
        )
        return JSONResponse(
            {
                "ok": result.ok,
                "connection": updated.to_dict() if updated else None,
                "metadata": result.metadata,
            }
        )

    @router.get("/connectors/agent-grants")
    async def list_grants(
        request: Request,
        connection_id: str | None = Query(default=None, alias="connectionId"),
        agent_id: str | None = Query(default=None, alias="agentId"),
    ) -> JSONResponse:
        await _require_admin(request)
        normalized_agent_id = (
            _normalize_agent_ref(agent_id, missing_ok=True) if agent_id is not None else None
        )
        if agent_id is not None and normalized_agent_id is None:
            return JSONResponse({"grants": []})
        return JSONResponse(
            {
                "grants": [
                    grant.to_dict()
                    for grant in get_connector_store().list_agent_grants(
                        connection_id=connection_id,
                        agent_id=normalized_agent_id,
                    )
                ]
            }
        )

    @router.post("/connectors/connections/{connection_id}/agent-grants")
    async def upsert_grant(
        request: Request,
        connection_id: str,
        body: AgentGrantBody,
    ) -> JSONResponse:
        await _require_admin(request)
        store = get_connector_store()
        conn = store.get_connection(connection_id)
        if conn is None:
            raise OmnigentError("connector connection not found", code=ErrorCode.NOT_FOUND)
        normalized_agent_id = _normalize_agent_ref(body.agent_id)
        registry = build_connector_registry()
        known_services = {svc.service_key: svc for svc in store.list_services(connection_id)}
        requested = _grant_targets(
            registry=registry,
            connection=conn,
            known_services=known_services,
            body=body,
        )
        requested_set = set(requested)
        grants = []
        for service_key, tool_key in requested:
            grants.append(
                store.upsert_agent_grant(
                    connection_id=connection_id,
                    agent_id=normalized_agent_id,
                    service_key=service_key,
                    tool_key=tool_key,
                    enabled=body.enabled,
                    status="active" if body.enabled else "disabled",
                )
            )
        if body.enabled and body.replace:
            for grant in store.list_agent_grants(
                connection_id=connection_id,
                agent_id=normalized_agent_id,
            ):
                if (grant.service_key, grant.tool_key) in requested_set:
                    continue
                store.upsert_agent_grant(
                    connection_id=connection_id,
                    agent_id=normalized_agent_id,
                    service_key=grant.service_key,
                    tool_key=grant.tool_key,
                    enabled=False,
                    status="disabled",
                )
        if body.materialize:
            materialize_agent_connector_grant(
                connection=conn,
                services=list(known_services.values()),
                grants=store.list_agent_grants(
                    connection_id=connection_id,
                    agent_id=normalized_agent_id,
                ),
                agent_id=normalized_agent_id,
            )
        return JSONResponse({"grants": [grant.to_dict() for grant in grants]})

    return router


__all__ = ["create_connectors_router"]
