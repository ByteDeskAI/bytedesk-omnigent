"""Integration secret readiness plan API route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_secret_plans_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the connected-app credential readiness compiler router."""

    router = APIRouter()

    @router.post("/integration-secret-plans/compile")
    async def compile_plan(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Compile a deterministic integration secret readiness plan."""

        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_secret_plans import (
            compile_integration_secret_plan,
        )

        try:
            plan = compile_integration_secret_plan(payload)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(plan.to_dict())

    return router
