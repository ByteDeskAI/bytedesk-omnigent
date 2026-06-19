"""Governance read API route (BDP-2278 F5 backbone, ADR-0142).

``GET /v1/governance/summary`` — the goals backlog + open deliberations rollup.
``GET /v1/governance/leaderboard?metric=`` — the outcome leaderboard for a metric.
Thin glue over the pure ``bytedesk_omnigent.governance`` read model + the durable stores.

This is founder/control-plane org data (goal backlog, open deliberations, per-agent
leaderboard), so — like every sibling read route — it is authenticated: in
multi-user mode an unauthenticated caller is rejected (401); single-user mode
(``auth_provider=None``) leaves it open (BDP-2289).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_governance_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the read-only governance router.

    :param auth_provider: Auth provider used to identify the requesting user.
        When set (multi-user mode) both handlers require a valid identity;
        ``None`` (single-user mode) leaves them open.
    """
    router = APIRouter()

    @router.get("/governance/summary")
    async def summary(request: Request) -> JSONResponse:
        """One-glance org state: goals by status + open deliberations."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.deliberation import get_deliberation_store
        from bytedesk_omnigent.goals import get_goal_store
        from bytedesk_omnigent.governance import governance_summary

        data = governance_summary(
            goal_store=get_goal_store(),
            deliberation_store=get_deliberation_store(),
        )
        return JSONResponse(data)

    @router.get("/governance/leaderboard")
    async def leaderboard(
        request: Request,
        metric: str,
        limit: int = Query(default=10, ge=1, le=100),
    ) -> JSONResponse:
        """The outcome leaderboard for a metric (find-specialist signal)."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.governance import outcome_leaderboard
        from bytedesk_omnigent.outcomes import get_outcome_ledger

        data = outcome_leaderboard(
            outcome_ledger=get_outcome_ledger(), metric=metric, limit=limit
        )
        return JSONResponse(data)

    return router
