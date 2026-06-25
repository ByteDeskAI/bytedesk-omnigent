"""Goals backlog/admin API route (BDP-2290, ADR-0142).

``GET /v1/goals`` remains the authenticated backlog snapshot. Admin mutations
add the missing organization/department/agent targeting and dependent readiness
frames used by the Omnigent goals overlay. This is Omnigent-only for now; the
Platform-facing contract is the realtime ``goal.changed`` delta plus REST
snapshot reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_user
from omnigent.stores.permission_store import PermissionStore


class GoalDependencyBody(BaseModel):
    """Create/update body for one goal dependency."""

    kind: str = Field(default="manual", max_length=32)
    ref: str | None = Field(default=None, max_length=256)
    label: str = Field(min_length=1, max_length=512)
    status: str = Field(default="pending", max_length=16)
    metadata: dict[str, Any] | None = None


class CreateGoalBody(BaseModel):
    """Admin create body for a scoped goal."""

    title: str = Field(min_length=1, max_length=512)
    priority: int = Field(default=3, ge=1, le=10)
    source: str | None = Field(default="admin", max_length=64)
    payload: dict[str, Any] | None = None
    target_kind: str = Field(default="organization", max_length=16)
    target_id: str | None = Field(default=None, max_length=128)
    target_label: str | None = Field(default=None, max_length=256)
    readiness_kind: str = Field(default="immediate", max_length=16)
    dependencies: list[GoalDependencyBody] = Field(default_factory=list)


class UpdateGoalBody(BaseModel):
    """Admin patch body for goal metadata and lifecycle state."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    priority: int | None = Field(default=None, ge=1, le=10)
    payload: dict[str, Any] | None = None
    status: str | None = Field(default=None, max_length=16)
    target_kind: str | None = Field(default=None, max_length=16)
    target_id: str | None = Field(default=None, max_length=128)
    target_label: str | None = Field(default=None, max_length=256)
    readiness_kind: str | None = Field(default=None, max_length=16)
    activation_state: str | None = Field(default=None, max_length=16)


class UpdateDependencyBody(BaseModel):
    """Admin patch body for a dependency."""

    kind: str | None = Field(default=None, max_length=32)
    ref: str | None = Field(default=None, max_length=256)
    label: str | None = Field(default=None, min_length=1, max_length=512)
    status: str | None = Field(default=None, max_length=16)
    metadata: dict[str, Any] | None = None


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    user_id = get_user_id(request, auth_provider)
    if permission_store is None:
        return user_id
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    if not await asyncio.to_thread(permission_store.is_admin, user_id):
        raise OmnigentError("Admin privileges required to manage goals", code=ErrorCode.FORBIDDEN)
    return user_id


def _invalid_input(exc: ValueError) -> OmnigentError:
    return OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT)


def _format_sse(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "message")
    data = json.dumps(event, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"


def create_goals_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the goals router.

    Reads require authentication in multi-user mode. Mutations require admin
    privileges when a permission store is available.
    """
    router = APIRouter()

    @router.get("/goals")
    async def list_goals(
        request: Request,
        status: str | None = None,
        owner: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        readiness_kind: str | None = None,
        activation_state: str | None = None,
        ready_only: bool = False,
        include_dependencies: bool = False,
    ) -> JSONResponse:
        """List the ops backlog (by priority then age), optionally filtered."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.goals import get_goal_store

        goals = get_goal_store().list_goals(
            status=status,
            owner_agent_id=owner,
            target_kind=target_kind,
            target_id=target_id,
            readiness_kind=readiness_kind,
            activation_state=activation_state,
            ready_only=ready_only,
            include_dependencies=include_dependencies,
        )
        return JSONResponse({"goals": [asdict(g) for g in goals]})

    @router.post("/goals")
    async def create_goal(request: Request, body: CreateGoalBody) -> JSONResponse:
        """Create a scoped goal from the admin overlay."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        try:
            goal = get_goal_store().create_goal(
                title=body.title,
                priority=body.priority,
                source=body.source,
                payload=body.payload,
                target_kind=body.target_kind,
                target_id=body.target_id,
                target_label=body.target_label,
                readiness_kind=body.readiness_kind,
                dependencies=[d.model_dump() for d in body.dependencies],
            )
        except ValueError as exc:
            raise _invalid_input(exc) from exc
        return JSONResponse({"goal": asdict(goal)}, status_code=201)

    @router.get("/goals/events", response_model=None)
    async def subscribe_goal_events(request: Request) -> StreamingResponse:
        """Subscribe to goal.changed events for live Omnigent admin refresh."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.goals import GOAL_EVENT_USER_KEY
        from omnigent.runtime.event_hub import subscribe

        async def _gen() -> AsyncIterator[str]:
            async for event in subscribe(
                GOAL_EVENT_USER_KEY,
                types={"goal.changed"},
                heartbeat_interval_s=20.0,
            ):
                yield _format_sse(event)
                if await request.is_disconnected():
                    break

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/goals/{goal_id}")
    async def get_goal(request: Request, goal_id: str) -> JSONResponse:
        """Return one scoped goal with dependencies."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.goals import get_goal_store

        goal = get_goal_store().get_goal(goal_id=goal_id)
        if goal is None:
            raise OmnigentError("goal not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"goal": asdict(goal)})

    @router.patch("/goals/{goal_id}")
    async def update_goal(request: Request, goal_id: str, body: UpdateGoalBody) -> JSONResponse:
        """Update a scoped goal from the admin overlay."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        updates = body.model_dump(exclude_unset=True)
        try:
            goal = get_goal_store().update_goal(goal_id=goal_id, **updates)
        except ValueError as exc:
            raise _invalid_input(exc) from exc
        if goal is None:
            raise OmnigentError("goal not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"goal": asdict(goal)})

    @router.post("/goals/{goal_id}/activate")
    async def activate_goal(request: Request, goal_id: str) -> JSONResponse:
        """Manual override: make a deferred/dependent goal claimable now."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        goal = get_goal_store().activate_goal(goal_id=goal_id)
        if goal is None:
            raise OmnigentError("goal not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"goal": asdict(goal)})

    @router.post("/goals/{goal_id}/dependencies")
    async def add_dependency(
        request: Request,
        goal_id: str,
        body: GoalDependencyBody,
    ) -> JSONResponse:
        """Attach a dependency to a goal."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        try:
            dependency = get_goal_store().add_dependency(
                goal_id=goal_id,
                kind=body.kind,
                ref=body.ref,
                label=body.label,
                status=body.status,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise _invalid_input(exc) from exc
        if dependency is None:
            raise OmnigentError("goal not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"dependency": asdict(dependency)}, status_code=201)

    @router.patch("/goals/{goal_id}/dependencies/{dependency_id}")
    async def update_dependency(
        request: Request,
        goal_id: str,
        dependency_id: str,
        body: UpdateDependencyBody,
    ) -> JSONResponse:
        """Update or resolve a dependency."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        updates = body.model_dump(exclude_unset=True)
        try:
            dependency = get_goal_store().update_dependency(
                goal_id=goal_id,
                dependency_id=dependency_id,
                **updates,
            )
        except ValueError as exc:
            raise _invalid_input(exc) from exc
        if dependency is None:
            raise OmnigentError("goal dependency not found", code=ErrorCode.NOT_FOUND)
        return JSONResponse({"dependency": asdict(dependency)})

    return router
