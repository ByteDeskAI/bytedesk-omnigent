"""Read/admin routes for the NATS runner fabric."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request, Response, status

from bytedesk_omnigent.fabric.outbox import (
    SqlAlchemyFabricOutboxStore,
    get_fabric_outbox_store,
)
from omnigent.fabric.policies import (
    InMemoryFabricCapacityPolicy,
    InMemoryQuarantinePolicy,
)
from omnigent.fabric.preflight import InMemoryFabricInspector
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_fabric_router(
    *,
    auth_provider: AuthProvider | None = None,
    inspector: InMemoryFabricInspector | None = None,
    outbox_store: SqlAlchemyFabricOutboxStore | None = None,
    capacity_policy: InMemoryFabricCapacityPolicy | None = None,
    quarantine_policy: InMemoryQuarantinePolicy | None = None,
) -> APIRouter:
    router = APIRouter()
    fabric_inspector = inspector or InMemoryFabricInspector()

    @router.get("/fabric/preflight", response_model=None)
    async def preflight(request: Request, response: Response) -> dict[str, Any]:
        require_user(request, auth_provider)
        report = await fabric_inspector.preflight()
        if report.status != "pass":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return report.to_dict()

    @router.get("/fabric/lanes", response_model=None)
    async def lanes(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        manifest = fabric_inspector.manifest()
        return {
            "object": "fabric_lane.list",
            "data": [
                {"lane": lane, "subject": manifest.lane_subject(lane)}
                for lane in manifest.lanes
            ],
        }

    @router.get("/fabric/topology", response_model=None)
    async def topology(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return fabric_inspector.manifest().to_topology()

    @router.get("/fabric/runners", response_model=None)
    async def runners(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"object": "fabric_runner.list", "data": []}

    @router.get("/fabric/pools", response_model=None)
    async def pools(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"object": "fabric_runner_pool.list", "data": []}

    @router.get("/fabric/capacity", response_model=None)
    async def capacity(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        data = (
            [record.to_dict() for record in capacity_policy.records()]
            if capacity_policy is not None
            else []
        )
        return {"object": "fabric_capacity.list", "data": data}

    @router.get("/fabric/dlq", response_model=None)
    async def dlq(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"object": "fabric_dlq.list", "data": []}

    @router.get("/fabric/outbox", response_model=None)
    async def outbox(
        request: Request,
        outbox_status: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        store = outbox_store or get_fabric_outbox_store()
        records = await asyncio.to_thread(
            store.recent,
            status=outbox_status,
            limit=limit,
        )
        return {
            "object": "fabric_outbox.list",
            "data": [record.to_summary() for record in records],
        }

    @router.get("/fabric/quarantine", response_model=None)
    async def quarantine(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        data = (
            [record.to_dict() for record in quarantine_policy.records()]
            if quarantine_policy is not None
            else []
        )
        return {"object": "fabric_quarantine.list", "data": data}

    @router.get("/fabric/timeline", response_model=None)
    async def timeline(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"object": "fabric_timeline.list", "data": []}

    @router.get("/fabric/audit", response_model=None)
    async def audit(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"object": "fabric_audit.list", "data": []}

    return router
