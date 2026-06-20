"""Integration workflow-plan API route.

``POST /v1/integration-workflow-plans/compile`` lets connected applications preview
an Archon-style deterministic harness plan before creating tasks or running an
agent.  It is authenticated like the other ByteDesk control-plane routes in
multi-user mode and open in single-user mode.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_workflow_plans_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the connected-app workflow-plan compiler router."""
    router = APIRouter()

    @router.post("/integration-workflow-plans/compile")
    async def compile_plan(request: Request) -> JSONResponse:
        """Compile a provider-neutral deterministic plan for one integration goal."""
        require_user(request, auth_provider)
        body = await request.json()

        from bytedesk_omnigent.integration_workflow_plans import (
            compile_integration_workflow_plan,
        )

        try:
            plan = compile_integration_workflow_plan(
                provider=body["provider"],
                goal=body["goal"],
                object_ref=body["object_ref"],
                requester=body.get("requester"),
                context_refs=body.get("context_refs"),
                idempotency_key=body.get("idempotency_key"),
                require_approval=body.get("require_approval"),
                writeback=body.get("writeback", True),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"missing required field: {exc.args[0]}",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return JSONResponse(plan.to_dict())

    return router
