"""Integration event route compiler API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class IntegrationEventRouteCompileRequest(BaseModel):
    """Request body for deterministic connected-app event routing previews."""

    provider: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    workspace_id: str | None = None
    desired_outcome: str | None = None
    writeback: bool = False


def create_integration_event_routes_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration event route compiler router."""
    router = APIRouter()

    @router.post("/integration-event-routes/compile")
    async def compile_route(
        request: Request,
        body: IntegrationEventRouteCompileRequest,
    ) -> JSONResponse:
        """Preview how an external app event maps to Omnigent execution."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_event_routes import compile_event_route

        plan = compile_event_route(
            provider=body.provider,
            event_type=body.event_type,
            subject_id=body.subject_id,
            workspace_id=body.workspace_id,
            desired_outcome=body.desired_outcome,
            writeback=body.writeback,
        )
        return JSONResponse({"plan": plan.to_dict()})

    return router
