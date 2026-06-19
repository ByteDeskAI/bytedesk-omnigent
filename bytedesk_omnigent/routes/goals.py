"""Goals backlog read API route (BDP-2290, ADR-0142).

``GET /v1/goals[?status=&owner=]`` — the ops backlog (BDP-2271 C3 goal store) the
Founder Governance cockpit (BDP-976) reads, proxied by ByteDesk.Office
``/api/office/goals``. Thin glue over the durable goal store; authenticated like
its governance sibling — 401 in multi-user mode, open in single-user mode
(``auth_provider=None``).
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_goals_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the read-only goals-backlog router.

    :param auth_provider: when set (multi-user) the handler requires a valid
        identity; ``None`` (single-user) leaves it open.
    """
    router = APIRouter()

    @router.get("/goals")
    async def list_goals(
        request: Request,
        status: str | None = None,
        owner: str | None = None,
    ) -> JSONResponse:
        """List the ops backlog (by priority then age), optionally filtered."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.goals import get_goal_store

        goals = get_goal_store().list_goals(status=status, owner_agent_id=owner)
        return JSONResponse({"goals": [asdict(g) for g in goals]})

    return router
