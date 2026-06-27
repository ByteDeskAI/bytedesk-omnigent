"""Connected-app provider routes (Phase 4, BDP-2586).

Two surfaces:

- ``POST /v1/goal-providers/register`` + ``GET /v1/goal-providers`` (admin-gated):
  a connected app registers/lists its :class:`ProviderManifest`. The app's own
  ``/goal-sensors/.../evaluate`` + ``/goal-actuators/.../execute`` endpoints are
  Phase 5 (platform side).
- ``POST /v1/inbound/events``: the **canonical inbound ingress** — the first LIVE
  caller of ADR-0155's ``pipeline.ingest``. Accepts an already-canonical event and
  runs the existing pipeline (wire-tap → idempotent claim → fan-out). Gated by the
  ``inbound.cutover.provider`` flag (chained on the master ``inbound.pipeline.enabled``),
  so it ships dark like every other cutover. ``outcome.booked`` events fan out to
  the :class:`OutcomeProcessor` → ``treasury.book_outcome`` (the flywheel input).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.stores.permission_store import PermissionStore


class ActuatorSpecBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    risk_tier: int = Field(default=2, ge=0, le=5)


class ProviderAuthBody(BaseModel):
    header: str = Field(min_length=1, max_length=128)
    secret: str | None = Field(default=None, max_length=2048)


class RegisterProviderBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    sensors: list[str] = Field(default_factory=list)
    actuators: list[ActuatorSpecBody] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    webhook_sources: list[str] = Field(default_factory=list)
    auth: ProviderAuthBody | None = None


class CanonicalEventBody(BaseModel):
    """An already-canonical inbound event posted by a connected app."""

    type: str = Field(min_length=1, max_length=128)
    source: str = Field(default="provider", max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=512)
    occurred_at: int | None = None
    tenant_id: str | None = Field(default=None, max_length=128)
    event_id: str | None = Field(default=None, max_length=256)
    normalized: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


def create_providers_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the provider register/list + canonical ingress router."""
    from bytedesk_omnigent.engine.providers.ingress import (
        CHANNEL_PROVIDER,
        register_canonical_translator,
        register_outcome_processor,
    )

    # Light up the canonical channel + the outcome sink once (idempotent).
    register_canonical_translator()
    register_outcome_processor()

    router = APIRouter()

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
                "Admin privileges required to manage providers", code=ErrorCode.FORBIDDEN
            )

    @router.post("/goal-providers/register")
    async def register_provider(request: Request, body: RegisterProviderBody) -> JSONResponse:
        """Register/replace a connected-app provider manifest (admin)."""
        await _require_admin(request)
        from bytedesk_omnigent.engine.providers.registry import (
            ProviderManifest,
            get_provider_registry,
        )

        manifest = ProviderManifest.from_dict(body.model_dump())
        get_provider_registry().register_provider(manifest)
        return JSONResponse({"provider": manifest.to_dict()}, status_code=201)

    @router.get("/goal-providers")
    async def list_providers(request: Request) -> JSONResponse:
        """List registered provider manifests (admin; auth secrets omitted)."""
        await _require_admin(request)
        from bytedesk_omnigent.engine.providers.registry import get_provider_registry

        return JSONResponse(
            {"providers": [m.to_dict() for m in get_provider_registry().providers()]}
        )

    @router.post("/inbound/events")
    async def ingest_canonical_event(request: Request, body: CanonicalEventBody) -> JSONResponse:
        """Canonical inbound ingress — the first live caller of pipeline.ingest."""
        await _require_admin(request)
        from bytedesk_omnigent.inbound.flags import (
            INBOUND_CUTOVER_PROVIDER,
            evaluate_inbound_flag,
        )

        if not await evaluate_inbound_flag(
            INBOUND_CUTOVER_PROVIDER, tenant=body.tenant_id, source=body.source
        ):
            return JSONResponse({"status": "disabled", "enabled": False}, status_code=202)

        from bytedesk_omnigent.inbound.pipeline import ingest
        from bytedesk_omnigent.inbound.processors import all_processors
        from bytedesk_omnigent.inbound.store import get_inbound_event_store

        result = ingest(
            channel=CHANNEL_PROVIDER,
            source=body.source,
            raw_payload=body.model_dump(),
            headers=dict(request.headers),
            store=get_inbound_event_store(),
            processors=all_processors(),
        )
        return JSONResponse(
            {
                "status": result.status,
                "idempotencyKey": result.idempotency_key,
                "eventType": result.event_type,
                "duplicate": result.duplicate,
                "detail": result.detail,
            },
            status_code=result.http_status,
        )

    return router


__all__ = ["create_providers_router"]
