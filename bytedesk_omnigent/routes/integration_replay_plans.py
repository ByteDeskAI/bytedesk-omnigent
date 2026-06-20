"""Integration replay plan route for connected-app onboarding."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_replay_plans_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the deterministic replay-plan compiler route."""

    router = APIRouter()

    @router.post("/integration-replay-plans/compile")
    async def compile_plan(request: Request) -> JSONResponse:
        """Compile retry/dedupe/approval/dead-letter behavior for one event."""

        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_replay_plans import (
            compile_integration_replay_plan,
        )

        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "JSON body must be an object"}, status_code=400)
        try:
            plan = compile_integration_replay_plan(payload)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(plan)

    return router
