"""Tasks backlog read API route (BDP-2333, ADR-0142).

``GET /v1/tasks[?status=&owner=&assignee=]`` — the durable tasks backlog (the
first-class-task store) the Founder Governance cockpit (BDP-976) reads, proxied by
ByteDesk.Office. Thin glue over the durable task store; authenticated like its
goals sibling — 401 in multi-user mode, open in single-user mode
(``auth_provider=None``).
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_tasks_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the read-only tasks-backlog router.

    :param auth_provider: when set (multi-user) the handler requires a valid
        identity; ``None`` (single-user) leaves it open.
    """
    router = APIRouter()

    @router.get("/tasks")
    async def list_tasks(
        request: Request,
        status: str | None = None,
        owner: str | None = None,
        assignee: str | None = None,
    ) -> JSONResponse:
        """List the tasks backlog (by priority then age), optionally filtered."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.tasks.store import get_task_store

        tasks = get_task_store().list_tasks(
            status=status,
            owner_agent_id=owner,
            assignee_agent_id=assignee,
        )
        return JSONResponse({"tasks": [asdict(t) for t in tasks]})

    return router
