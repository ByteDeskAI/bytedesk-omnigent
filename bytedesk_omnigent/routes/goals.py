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
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_user
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)

PLANNING_EVENT_TYPES = {
    "goal.changed",
    "goal.planning.started",
    "goal.draft.updated",
    "goal.planning.committed",
}

PLANNER_AGENT_NAMES = ("goal-planner", "chief-of-staff")


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


class GoalPlannerSource(BaseModel):
    """A knowledge source the goal-planner assistant may reference."""

    id: str
    label: str
    available: bool = True
    tools: list[str] = Field(default_factory=list)
    reason: str | None = None


class StartGoalPlanningSessionBody(BaseModel):
    """Start a goal-planning interview for one scope."""

    target_kind: str = Field(max_length=16)
    target_id: str = Field(min_length=1, max_length=128)
    target_label: str | None = Field(default=None, max_length=256)
    source_ids: list[str] = Field(default_factory=list, max_length=8)


class GoalDraftBody(BaseModel):
    """Structured goal draft committed by the planner interview."""

    title: str = Field(min_length=1, max_length=512)
    priority: int = Field(default=3, ge=1, le=10)
    target_kind: str = Field(default="organization", max_length=16)
    target_id: str | None = Field(default=None, max_length=128)
    target_label: str | None = Field(default=None, max_length=256)
    readiness_kind: str = Field(default="immediate", max_length=16)
    dependencies: list[GoalDependencyBody] = Field(default_factory=list)
    outcome: str | None = Field(default=None, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    source_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any] | None = None


class CommitGoalPlanningSessionBody(BaseModel):
    """Commit a planner-produced draft into the durable goal backlog."""

    source_ids: list[str] = Field(default_factory=list, max_length=8)
    draft: GoalDraftBody


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


def _planner_sources() -> list[GoalPlannerSource]:
    google_available = bool(
        os.getenv("GOOGLE_WORKSPACE_MCP_URL") or os.getenv("BYTEDESK_GOOGLE_WORKSPACE_MCP_URL")
    )
    return [
        GoalPlannerSource(
            id="jira",
            label="Jira",
            tools=["bytedesk_jira"],
            available=True,
        ),
        GoalPlannerSource(
            id="confluence",
            label="Confluence",
            tools=["bytedesk_confluence"],
            available=True,
        ),
        GoalPlannerSource(
            id="google_workspace",
            label="Google Workspace",
            tools=["google_workspace"],
            available=google_available,
            reason=None if google_available else "not_configured",
        ),
    ]


def _available_sources(source_ids: list[str]) -> list[GoalPlannerSource]:
    by_id = {source.id: source for source in _planner_sources()}
    selected: list[GoalPlannerSource] = []
    for source_id in source_ids:
        source = by_id.get(source_id)
        if source is not None and source.available:
            selected.append(source)
    return selected


def _publish_planning_event(
    event_type: str,
    *,
    planning_session_id: str,
    target_kind: str,
    target_id: str,
    target_label: str | None,
    source_ids: list[str],
    goal_id: str | None = None,
    draft_ready: bool | None = None,
    occurred_at: int | None = None,
) -> None:
    event: dict[str, Any] = {
        "type": event_type,
        "planningSessionId": planning_session_id,
        "targetKind": target_kind,
        "targetId": target_id,
        "targetLabel": target_label,
        "sourceIds": source_ids,
        "occurredAt": occurred_at if occurred_at is not None else now_epoch(),
    }
    if goal_id is not None:
        event["goalId"] = goal_id
    if draft_ready is not None:
        event["draftReady"] = draft_ready
    try:
        from bytedesk_omnigent.realtime.bridge import emit_goal_planning

        emit_goal_planning(event)
    except Exception:  # pragma: no cover - best-effort bridge
        logger.exception("failed to publish goal-planning realtime delta")
    try:
        from bytedesk_omnigent.goals import GOAL_EVENT_USER_KEY
        from omnigent.runtime.event_hub import publish

        publish(GOAL_EVENT_USER_KEY, event)
    except Exception:  # pragma: no cover - best-effort local event stream
        logger.exception("failed to publish goal-planning event-hub delta")


def _resolve_planner_agent() -> Any:
    from omnigent.runtime import get_agent_store

    agent_store = get_agent_store()
    for name in PLANNER_AGENT_NAMES:
        agent = agent_store.get_by_name(name)
        if agent is not None:
            return agent
    expected = ", ".join(PLANNER_AGENT_NAMES)
    raise OmnigentError(
        f"No goal planner agent is registered; expected one of: {expected}",
        code=ErrorCode.NOT_FOUND,
    )


def _planner_prompt(
    *,
    target_kind: str,
    target_id: str,
    target_label: str | None,
    sources: list[GoalPlannerSource],
) -> str:
    source_labels = ", ".join(source.label for source in sources) or "none"
    label = target_label or target_id
    return (
        "GOAL PLANNING INTERVIEW\n"
        f"Scope: {target_kind}:{target_id} ({label})\n"
        f"Sources enabled: {source_labels}\n\n"
        "Run a planning interview for this scope. Ask one concise question at a time. "
        "When the harness exposes AskUserQuestion, use it for choices or required "
        "clarifications; otherwise ask directly in chat. Search/read the enabled "
        "sources before creating or recommending tracked work. The final draft must "
        "include title, priority, readiness_kind, dependencies, desired outcome, "
        "acceptance criteria, assumptions, and source references. Do not commit a "
        "goal until the user explicitly approves the final draft."
    )


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

    @router.get("/goals/planner/sources")
    async def list_planner_sources(request: Request) -> JSONResponse:
        """List available knowledge sources for the goal-planning assistant."""
        require_user(request, auth_provider)
        return JSONResponse({"sources": [source.model_dump() for source in _planner_sources()]})

    @router.post("/goals/planner/sessions")
    async def start_planning_session(
        request: Request,
        body: StartGoalPlanningSessionBody,
    ) -> JSONResponse:
        """Create an assistant-driven goal-planning session for one scope."""
        user_id = await _require_admin(request, auth_provider, permission_store)
        from omnigent.runtime import get_conversation_store

        agent = _resolve_planner_agent()
        sources = _available_sources(body.source_ids)
        source_ids = [source.id for source in sources]
        title = f"Plan goal: {body.target_label or body.target_id}"
        prompt = _planner_prompt(
            target_kind=body.target_kind,
            target_id=body.target_id,
            target_label=body.target_label,
            sources=sources,
        )
        conversation_store = get_conversation_store()
        conv = await asyncio.to_thread(
            conversation_store.create_conversation,
            agent_id=agent.id,
            title=title,
            kind="default",
        )
        labels = {
            "bytedesk.goal_planner": "1",
            "bytedesk.goal_planner.target_kind": body.target_kind,
            "bytedesk.goal_planner.target_id": body.target_id,
            "bytedesk.goal_planner.target_label": body.target_label or "",
            "bytedesk.goal_planner.sources": ",".join(source_ids),
        }
        await asyncio.to_thread(conversation_store.set_labels, conv.id, labels)
        await asyncio.to_thread(
            conversation_store.append,
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="seed",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": prompt}],
                    ),
                    created_by=user_id,
                )
            ],
        )
        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.grant, user_id, conv.id, LEVEL_OWNER)
        _publish_planning_event(
            "goal.planning.started",
            planning_session_id=conv.id,
            target_kind=body.target_kind,
            target_id=body.target_id,
            target_label=body.target_label,
            source_ids=source_ids,
            draft_ready=False,
        )
        return JSONResponse(
            {
                "session_id": conv.id,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "title": title,
                "prompt": prompt,
                "sources": [source.model_dump() for source in sources],
                "web_path": f"/c/{conv.id}",
            },
            status_code=201,
        )

    @router.post("/goals/planner/sessions/{session_id}/commit")
    async def commit_planning_session(
        request: Request,
        session_id: str,
        body: CommitGoalPlanningSessionBody,
    ) -> JSONResponse:
        """Commit an approved planner draft into the durable goal backlog."""
        await _require_admin(request, auth_provider, permission_store)
        from bytedesk_omnigent.goals import get_goal_store

        draft = body.draft
        payload = {
            **(draft.payload or {}),
            "goal_planning": {
                "session_id": session_id,
                "outcome": draft.outcome,
                "acceptance_criteria": draft.acceptance_criteria,
                "assumptions": draft.assumptions,
                "source_refs": draft.source_refs,
                "source_ids": body.source_ids,
            },
        }
        try:
            goal = get_goal_store().create_goal(
                title=draft.title,
                priority=draft.priority,
                source="goal-planner",
                payload=payload,
                target_kind=draft.target_kind,
                target_id=draft.target_id,
                target_label=draft.target_label,
                readiness_kind=draft.readiness_kind,
                dependencies=[dependency.model_dump() for dependency in draft.dependencies],
            )
        except ValueError as exc:
            raise _invalid_input(exc) from exc
        _publish_planning_event(
            "goal.planning.committed",
            planning_session_id=session_id,
            target_kind=goal.target_kind,
            target_id=goal.target_id,
            target_label=goal.target_label,
            source_ids=body.source_ids,
            goal_id=goal.id,
            draft_ready=True,
        )
        return JSONResponse({"goal": asdict(goal)}, status_code=201)

    @router.get("/goals/events", response_model=None)
    async def subscribe_goal_events(request: Request) -> StreamingResponse:
        """Subscribe to goal events for live Omnigent admin refresh."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.goals import GOAL_EVENT_USER_KEY
        from omnigent.runtime.event_hub import subscribe

        async def _gen() -> AsyncIterator[str]:
            async for event in subscribe(
                GOAL_EVENT_USER_KEY,
                types=PLANNING_EVENT_TYPES,
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
