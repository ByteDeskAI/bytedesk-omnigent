"""Tasks backlog API route (BDP-2333, ADR-0142).

``GET /v1/tasks[?status=&owner=&assignee=]`` — the durable tasks backlog (the
first-class-task store) the Founder Governance cockpit (BDP-976) reads, proxied by
ByteDesk.Office. ``POST /v1/tasks/intake`` lets connected apps normalize external
work items into first-class Tasks. Thin glue over the durable task store;
authenticated like its goals sibling — 401 in multi-user mode, open in single-user
mode (``auth_provider=None``).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def _task_payload(task) -> dict[str, Any]:
    """JSON response shape for a Task dataclass."""

    data = asdict(task)
    data["status"] = str(task.status)
    return data


def create_tasks_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the tasks-backlog router.

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
        return JSONResponse({"tasks": [_task_payload(t) for t in tasks]})

    @router.post("/tasks/intake")
    async def intake_work_item(
        request: Request,
        source: str | None = None,
    ) -> JSONResponse:
        """Normalize an external work item into an idempotent Omnigent Task.

        This is the first deterministic bridge for OAuth/webhook-backed work
        trackers (GitHub, Linear, Jira, Trello, and generic connected apps):
        callers submit provider payloads and Omnigent creates or returns the
        corresponding backlog Task without calling external services.
        """

        require_user(request, auth_provider)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "invalid_payload", "detail": "expected a JSON object"},
                status_code=400,
            )

        from bytedesk_omnigent.tasks.store import get_task_store
        from bytedesk_omnigent.work_item_intake import ingest_work_item

        try:
            result = ingest_work_item(
                payload=payload,
                source=source,
                store=get_task_store(),
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_payload", "detail": str(exc)},
                status_code=400,
            )

        return JSONResponse(
            {
                "status": "created" if result.created else "existing",
                "created": result.created,
                "provider": result.draft.provider,
                "external_id": result.draft.external_id,
                "task": _task_payload(result.task),
            },
            status_code=201 if result.created else 200,
        )

    return router
