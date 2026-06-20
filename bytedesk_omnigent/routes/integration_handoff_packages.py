"""Integration handoff package compile route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class IntegrationHandoffPackageCompileRequest(BaseModel):
    """Request body for compiling an external-event handoff package."""

    provider: str
    workspace_id: str
    event_type: str
    external_id: str
    actor: str | None = None
    title: str | None = None
    url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_capabilities: list[str] = Field(default_factory=list)


def create_integration_handoff_packages_router() -> APIRouter:
    """Build the deterministic handoff-package compiler router."""
    router = APIRouter()

    @router.post("/integration-handoff-packages/compile")
    async def compile_package(
        request: IntegrationHandoffPackageCompileRequest,
    ) -> JSONResponse:
        """Compile an agent-ready package from a third-party event descriptor."""
        from bytedesk_omnigent.integration_handoff_packages import (
            compile_integration_handoff_package,
        )

        package = compile_integration_handoff_package(
            provider=request.provider,
            workspace_id=request.workspace_id,
            event_type=request.event_type,
            external_id=request.external_id,
            actor=request.actor,
            title=request.title,
            url=request.url,
            payload=request.payload,
            requested_capabilities=request.requested_capabilities,
        )
        return JSONResponse({"handoff_package": package.to_dict()})

    return router
