"""Governance read API route (BDP-2278 F5 backbone, ADR-0142).

``GET /v1/governance/summary`` — the goals backlog + open deliberations rollup.
``GET /v1/governance/leaderboard?metric=`` — the outcome leaderboard for a metric.
Thin glue over the pure ``omnigent.governance`` read model + the durable stores.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def create_governance_router() -> APIRouter:
    """Build the read-only governance router."""
    router = APIRouter()

    @router.get("/governance/summary")
    async def summary() -> JSONResponse:
        """One-glance org state: goals by status + open deliberations."""
        from omnigent.deliberation import get_deliberation_store
        from omnigent.goals import get_goal_store
        from omnigent.governance import governance_summary

        data = governance_summary(
            goal_store=get_goal_store(),
            deliberation_store=get_deliberation_store(),
        )
        return JSONResponse(data)

    @router.get("/governance/leaderboard")
    async def leaderboard(metric: str, limit: int = 10) -> JSONResponse:
        """The outcome leaderboard for a metric (find-specialist signal)."""
        from omnigent.governance import outcome_leaderboard
        from omnigent.outcomes import get_outcome_ledger

        data = outcome_leaderboard(
            outcome_ledger=get_outcome_ledger(), metric=metric, limit=limit
        )
        return JSONResponse(data)

    return router
