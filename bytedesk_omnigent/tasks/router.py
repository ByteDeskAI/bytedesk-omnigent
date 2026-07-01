"""Tasks backlog read API route (BDP-2333, ADR-0142).

``GET /v1/tasks[?status=&owner=&assignee=]`` — the durable tasks backlog (the
first-class-task store) the Founder Governance cockpit (BDP-976) reads, proxied by
ByteDesk.Office. Thin glue over the durable task store; authenticated like its
goals sibling — 401 in multi-user mode, open in single-user mode
(``auth_provider=None``).
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.server.agent_refs import resolve_agent_ref
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class CreateTaskBody(BaseModel):
    """Create a reusable task/workflow template from the schedules UI."""

    title: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=12000)
    owner_agent_id: str | None = Field(default=None, max_length=128)
    required_capability: str | None = Field(default=None, max_length=128)
    priority: int = Field(default=3, ge=1, le=10)
    source: str = Field(default="schedule-workflow-plan", max_length=80)
    payload: dict[str, Any] | None = None


class RunTaskBody(BaseModel):
    """Optional dispatch override when a caller runs a task directly."""

    run_as_agent_id: str | None = Field(default=None, max_length=128)
    prompt: str | None = Field(default=None, max_length=12000)
    external_key: str | None = Field(default=None, max_length=256)


def create_tasks_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the read-only tasks-backlog router.

    :param auth_provider: when set (multi-user) the handler requires a valid
        identity; ``None`` (single-user) leaves it open.
    """
    router = APIRouter()

    def _normalize_agent_ref(agent_ref: str, *, missing_ok: bool = False) -> str | None:
        ref = agent_ref.strip()
        if not ref:
            return None if missing_ok else agent_ref
        try:
            from omnigent.runtime import get_agent_store

            agent = resolve_agent_ref(get_agent_store(), ref, template_only=True)
        except Exception as exc:
            if ref.startswith("ag_"):
                return ref
            if missing_ok:
                return None
            raise HTTPException(status_code=404, detail="agent not found") from exc
        if agent is not None:
            return agent.id
        if ref.startswith("ag_"):
            return ref
        if missing_ok:
            return None
        raise HTTPException(status_code=404, detail="agent not found")

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

        owner_agent_id = _normalize_agent_ref(owner, missing_ok=True) if owner else None
        assignee_agent_id = _normalize_agent_ref(assignee, missing_ok=True) if assignee else None
        if (owner and owner_agent_id is None) or (assignee and assignee_agent_id is None):
            return JSONResponse({"tasks": []})
        tasks = get_task_store().list_tasks(
            status=status,
            owner_agent_id=owner_agent_id,
            assignee_agent_id=assignee_agent_id,
        )
        return JSONResponse({"tasks": [asdict(t) for t in tasks]})

    @router.post("/tasks")
    async def create_task(request: Request, body: CreateTaskBody) -> JSONResponse:
        """Create a reusable workflow/task template."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.tasks.store import get_task_store

        payload = dict(body.payload or {})
        payload.setdefault("kind", "workflow-plan")
        payload["prompt"] = body.prompt.strip()
        task = get_task_store().create_task(
            title=body.title.strip(),
            priority=body.priority,
            source=body.source,
            required_capability=body.required_capability,
            payload=payload,
        )
        if body.owner_agent_id:
            owner_agent_id = _normalize_agent_ref(body.owner_agent_id)
            get_task_store().claim_task(
                task_id=task.id,
                owner_agent_id=owner_agent_id,
            )
            task = get_task_store().get_task(task.id) or task
        return JSONResponse({"task": asdict(task)}, status_code=201)

    @router.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> JSONResponse:
        """Return one task template/backlog item."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.tasks.store import get_task_store

        task = get_task_store().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return JSONResponse({"task": asdict(task)})

    @router.post("/tasks/{task_id}/run")
    async def run_task_endpoint(
        request: Request,
        task_id: str,
        body: RunTaskBody | None = None,
    ) -> JSONResponse:
        """Run one task through the existing resolve -> session-dispatch seam."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.task_execution import TaskDispatchError, run_task
        from bytedesk_omnigent.tasks.store import get_task_store

        task = get_task_store().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        body = body or RunTaskBody()
        if body.prompt:
            payload = dict(task.payload or {})
            payload["prompt"] = body.prompt.strip()
            task = replace(task, payload=payload)
        if body.run_as_agent_id:
            task = replace(task, owner_agent_id=_normalize_agent_ref(body.run_as_agent_id))
        try:
            dispatch = run_task(task, external_key=body.external_key)
        except TaskDispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"dispatch": asdict(dispatch)})

    return router
