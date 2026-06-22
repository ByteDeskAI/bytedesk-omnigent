"""Integration workflow harness compiler route.

``GET /v1/integration-workflow-harness`` exposes the pure deterministic harness
compiler to ByteDesk Platform and embedded third-party application setup flows.
It does not touch secrets or external APIs; it only returns the phase contract an
agent run must satisfy before mutating the provider.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from bytedesk_omnigent.integration_harness import compile_integration_harness


def create_integration_harness_router() -> APIRouter:
    """Build the read-only integration harness compiler router."""
    router = APIRouter()

    @router.get("/integration-workflow-harness")
    async def integration_workflow_harness(
        provider: str = Query(..., min_length=1),
        objective: str = Query(..., min_length=1),
        agent_id: str = Query(..., min_length=1),
        external_object: str = Query(..., min_length=1),
    ) -> JSONResponse:
        """Compile a deterministic third-party integration workflow contract."""
        plan = compile_integration_harness(
            provider=provider,
            objective=objective,
            agent_id=agent_id,
            external_object=external_object,
        )
        return JSONResponse(plan.to_dict())

    return router
