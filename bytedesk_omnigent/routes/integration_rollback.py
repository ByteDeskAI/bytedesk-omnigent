"""API route for deterministic third-party integration rollback plans."""

from __future__ import annotations

from fastapi import APIRouter, Query

from bytedesk_omnigent.integration_rollback import compile_integration_rollback_plan


def create_integration_rollback_router() -> APIRouter:
    """Build the read-only integration rollback planning router."""
    router = APIRouter()

    @router.get("/integration-rollback-plan")
    async def integration_rollback_plan(
        provider: str = Query(..., min_length=1),
        operation: str = Query(..., min_length=1),
        agent_id: str = Query(..., min_length=1),
        external_ref: str = Query(..., min_length=1),
        mutation_summary: str = "",
        risk_level: str = "medium",
    ) -> dict:
        """Return a pure, deterministic rollback plan for an external mutation."""
        return compile_integration_rollback_plan(
            provider=provider,
            operation=operation,
            agent_id=agent_id,
            external_ref=external_ref,
            mutation_summary=mutation_summary,
            risk_level=risk_level,
        ).to_dict()

    return router
