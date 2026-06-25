"""Scoped Skills Concierge session bootstrap (mirrors goals planner sessions)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_user
from omnigent.stores.permission_store import PermissionStore

CONCIERGE_AGENT_NAMES = ("skills-concierge",)


class StartSkillsConciergeSessionBody(BaseModel):
    target_kind: str = Field(default="organization", max_length=16)
    target_id: str = Field(min_length=1, max_length=128)
    target_label: str | None = Field(default=None, max_length=256)
    target_agent_ids: list[str] = Field(default_factory=list)


def _resolve_concierge_agent() -> Any:
    from omnigent.runtime import get_agent_store

    agent_store = get_agent_store()
    for name in CONCIERGE_AGENT_NAMES:
        agent = agent_store.get_by_name(name)
        if agent is not None:
            return agent
    expected = ", ".join(CONCIERGE_AGENT_NAMES)
    raise OmnigentError(
        f"No skills concierge agent is registered; expected one of: {expected}",
        code=ErrorCode.NOT_FOUND,
    )


def _scope_phrase(target_kind: str, target_id: str) -> str:
    if target_kind == "organization":
        return "organization"
    if target_kind == "department":
        return f"department:{target_id}"
    if target_kind == "employee":
        return f"employee:{target_id}"
    return target_id


def _concierge_prompt(
    *,
    target_kind: str,
    target_id: str,
    target_label: str | None,
    target_agent_ids: list[str],
) -> str:
    label = target_label or target_id
    scope = _scope_phrase(target_kind, target_id)
    agent_summary = (
        f"{len(target_agent_ids)} target agent(s): {', '.join(target_agent_ids)}"
        if target_agent_ids
        else "resolve targets with sys_skill_resolve_targets"
    )
    return (
        "SKILLS CONCIERGE SESSION\n"
        f"Scope: {target_kind}:{target_id} ({label})\n"
        f"Resolve scope phrase: {scope}\n"
        f"Targets: {agent_summary}\n\n"
        "The operator opened the Skills page for this scope. Greet them briefly, "
        "confirm the install scope with sys_skill_resolve_targets, and help them "
        "search the ByteDesk catalog (github_marketplace) or other sources, then "
        "run the install saga when they pick a skill."
    )


def create_skills_concierge_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/skills/concierge/sessions")
    async def start_skills_concierge_session(
        request: Request,
        body: StartSkillsConciergeSessionBody,
    ) -> JSONResponse:
        user_id = require_user(request, auth_provider)
        from omnigent.runtime import get_conversation_store

        agent = _resolve_concierge_agent()
        title = f"Skills: {body.target_label or body.target_id}"
        prompt = _concierge_prompt(
            target_kind=body.target_kind,
            target_id=body.target_id,
            target_label=body.target_label,
            target_agent_ids=body.target_agent_ids,
        )
        conversation_store = get_conversation_store()
        conv = await asyncio.to_thread(
            conversation_store.create_conversation,
            agent_id=agent.id,
            title=title,
            kind="default",
        )
        labels = {
            "bytedesk.skills_concierge": "1",
            "bytedesk.skills_concierge.target_kind": body.target_kind,
            "bytedesk.skills_concierge.target_id": body.target_id,
            "bytedesk.skills_concierge.target_label": body.target_label or "",
            "bytedesk.skills_concierge.scope": _scope_phrase(
                body.target_kind,
                body.target_id,
            ),
            "bytedesk.skills_concierge.target_agent_ids": ",".join(body.target_agent_ids),
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
        return JSONResponse(
            {
                "session_id": conv.id,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "title": title,
                "prompt": prompt,
                "web_path": f"/c/{conv.id}",
            },
            status_code=201,
        )

    return router