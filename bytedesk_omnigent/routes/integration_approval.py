"""Connected-app approval planning routes for ByteDesk Platform."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_approval_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the deterministic integration approval-plan compiler route."""
    router = APIRouter()

    @router.post("/integration-approval-plans/compile")
    async def compile_approval_plan(request: Request) -> JSONResponse:
        """Preview OAuth/service approval gates before installing a connected app."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_approval import (
            compile_integration_approval_plan,
        )

        payload = await request.json()
        try:
            plan = compile_integration_approval_plan(
                provider=str(payload.get("provider") or ""),
                scopes=list(payload.get("scopes") or []),
                requested_operations=list(payload.get("requested_operations") or []),
                writeback_enabled=bool(payload.get("writeback_enabled", False)),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse(
                {"status": "invalid_request", "detail": str(exc)}, status_code=400
            )
        return JSONResponse({"approval_plan": plan.to_dict()})

    return router
