"""Skill acquisition routes for template-agent images."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.skills.acquisition import (
    InstallMode,
    SkillAcquisitionService,
    SkillApplyResult,
    SkillCommandSpec,
    SkillPreview,
    SkillSearchHit,
)
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

#: Skill sources that run an OPERATOR-SUPPLIED command (arbitrary shell in a
#: staging workspace). They stay authenticated even on the otherwise-anonymous
#: read routes — an unauthenticated caller must never reach arbitrary execution.
_COMMAND_SOURCES = frozenset({"freeform", "configured"})


class SkillCommandBody(BaseModel):
    """Command source for free-form/configured source adapters."""

    argv: list[str] | None = None
    shell: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=600)

    @model_validator(mode="after")
    def _one_command_shape(self) -> SkillCommandBody:
        if bool(self.argv) == bool(self.shell):
            raise ValueError("exactly one of argv or shell is required")
        return self

    def to_spec(self) -> SkillCommandSpec:
        return SkillCommandSpec(
            argv=tuple(self.argv) if self.argv is not None else None,
            shell=self.shell,
            timeout_seconds=self.timeout_seconds,
        )


class SkillSourceObject(BaseModel):
    id: str
    label: str
    kind: str
    supports_search: bool
    supports_preview: bool
    high_risk: bool
    available: bool = True
    unavailable_reason: str | None = None


class SkillSourcesResponse(BaseModel):
    object: str = "skill_source.list"
    data: list[SkillSourceObject]


class SkillSearchRequest(BaseModel):
    query: str
    sources: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    command: SkillCommandBody | None = None


class SkillSearchResultObject(BaseModel):
    source: str
    name: str
    description: str | None = None
    source_ref: str | None = None
    version: str | None = None
    url: str | None = None


class SkillSearchResponse(BaseModel):
    object: str = "skill_search.result"
    data: list[SkillSearchResultObject]
    errors: list[str] = Field(default_factory=list)


class InstalledSkillAgentObject(BaseModel):
    id: str
    name: str
    version: int


class InstalledSkillObject(BaseModel):
    name: str
    description: str
    agents: list[InstalledSkillAgentObject]


class InstalledSkillsResponse(BaseModel):
    object: str = "installed_skill.list"
    data: list[InstalledSkillObject]


class SkillFileManifestObject(BaseModel):
    path: str
    size: int
    sha256: str
    binary: bool


class StagedSkillObject(BaseModel):
    name: str
    description: str
    total_bytes: int
    files: list[SkillFileManifestObject]


class SkillCommandEvidenceObject(BaseModel):
    command: list[str] | str
    shell: bool
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str


class SkillTargetActionObject(BaseModel):
    agent_id: str
    agent_name: str
    agent_version: int
    skill_name: str
    action: str
    reason: str | None = None


class SkillPreviewRequest(BaseModel):
    operation: Literal["install", "remove"] = "install"
    target_agent_ids: list[str]
    install_mode: InstallMode = "replace"
    source: str = "freeform"
    source_ref: str | None = None
    command: SkillCommandBody | None = None
    selected_skill_names: list[str] | None = None
    skill_names: list[str] | None = None


class SkillPreviewResponse(BaseModel):
    object: str = "skill_preview"
    id: str
    operation: str
    install_mode: str
    created_at: int
    expires_at: int
    skills: list[StagedSkillObject]
    target_actions: list[SkillTargetActionObject]
    command: SkillCommandEvidenceObject | None = None
    skill_names: list[str] = Field(default_factory=list)


class SkillApplyRequest(BaseModel):
    target_agent_ids: list[str] | None = None


class SkillApplyResultObject(BaseModel):
    agent_id: str
    status: str
    action_count: int = 0
    version: int | None = None
    error: str | None = None


class SkillApplyResponse(BaseModel):
    object: str = "skill_apply.result"
    data: list[SkillApplyResultObject]


def create_skills_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    artifact_store: ArtifactStore | None,
    *,
    auth_provider: AuthProvider | None = None,
    service: SkillAcquisitionService | None = None,
) -> APIRouter:
    """Build the routes for the skill acquisition framework."""
    router = APIRouter()
    acquisition = service or SkillAcquisitionService(
        agent_store=agent_store,
        agent_cache=agent_cache,
        artifact_store=artifact_store,
    )

    @router.get("/skills/sources")
    async def list_skill_sources() -> SkillSourcesResponse:
        # Read-only adapter metadata (which sources exist + are available). No
        # user data, no mutation, no command execution — open to anonymous
        # service callers (the Skills Concierge's in-cluster MCP front, which
        # by design holds no server credential) so it can discover sources.
        return SkillSourcesResponse(
            data=[SkillSourceObject(**source) for source in acquisition.sources()]
        )

    @router.get("/skills/installed")
    async def list_installed_skills(
        request: Request,
        agent_id: str | None = None,
    ) -> InstalledSkillsResponse:
        _require_user(request, auth_provider)
        rows = await asyncio.to_thread(acquisition.installed, agent_id=agent_id)
        return InstalledSkillsResponse(
            data=[InstalledSkillObject.model_validate(row) for row in rows]
        )

    @router.post("/skills/search")
    async def search_skills(
        request: Request,
        body: SkillSearchRequest,
    ) -> SkillSearchResponse:
        # Registry discovery is read-only (no agent mutation), so anonymous
        # service callers (the Concierge's MCP front) may search the safe NAMED
        # adapters (skills/npm/github). The freeform/configured sources run an
        # operator-supplied command, so those — and any explicit `command` —
        # still require an authenticated user (a no-op in single-user mode; a
        # 401 for a multi-user anonymous caller). Never expose arbitrary
        # execution to an unauthenticated caller.
        if body.command is not None or _COMMAND_SOURCES.intersection(body.sources or ()):
            _require_user(request, auth_provider)
        outcome = await asyncio.to_thread(
            acquisition.search,
            body.query,
            sources=body.sources,
            limit=body.limit,
            command=body.command.to_spec() if body.command is not None else None,
        )
        return SkillSearchResponse(
            data=[_search_hit_to_response(hit) for hit in outcome.results],
            errors=list(outcome.errors),
        )

    @router.post("/skills/previews")
    async def create_skill_preview(
        request: Request,
        body: SkillPreviewRequest,
    ) -> SkillPreviewResponse:
        _require_user(request, auth_provider)
        preview = await asyncio.to_thread(
            acquisition.create_preview,
            operation=body.operation,
            target_agent_ids=body.target_agent_ids,
            install_mode=body.install_mode,
            source=body.source,
            source_ref=body.source_ref,
            command=body.command.to_spec() if body.command is not None else None,
            selected_skill_names=body.selected_skill_names,
            skill_names=body.skill_names,
        )
        return _preview_to_response(preview)

    @router.post("/skills/previews/{preview_id}/apply")
    async def apply_skill_preview(
        request: Request,
        preview_id: str,
        body: SkillApplyRequest | None = None,
    ) -> SkillApplyResponse:
        _require_user(request, auth_provider)
        results = await asyncio.to_thread(
            acquisition.apply_preview,
            preview_id,
            agent_ids=body.target_agent_ids if body is not None else None,
        )
        return SkillApplyResponse(data=[_apply_result_to_response(result) for result in results])

    return router


def _search_hit_to_response(hit: SkillSearchHit) -> SkillSearchResultObject:
    return SkillSearchResultObject(
        source=hit.source,
        name=hit.name,
        description=hit.description,
        source_ref=hit.source_ref,
        version=hit.version,
        url=hit.url,
    )


def _preview_to_response(preview: SkillPreview) -> SkillPreviewResponse:
    return SkillPreviewResponse(
        id=preview.id,
        operation=preview.operation,
        install_mode=preview.install_mode,
        created_at=preview.created_at,
        expires_at=preview.expires_at,
        skills=[
            StagedSkillObject(
                name=package.name,
                description=package.description,
                total_bytes=package.total_bytes,
                files=[SkillFileManifestObject(**file.__dict__) for file in package.files],
            )
            for package in preview.packages
        ],
        target_actions=[
            SkillTargetActionObject(
                agent_id=action.agent_id,
                agent_name=action.agent_name,
                agent_version=action.agent_version,
                skill_name=action.skill_name,
                action=action.action,
                reason=action.reason,
            )
            for action in preview.target_actions
        ],
        command=(
            SkillCommandEvidenceObject(**preview.command.__dict__)
            if preview.command
            else None
        ),
        skill_names=list(preview.skill_names),
    )


def _apply_result_to_response(result: SkillApplyResult) -> SkillApplyResultObject:
    return SkillApplyResultObject(
        agent_id=result.agent_id,
        status=result.status,
        action_count=result.action_count,
        version=result.version,
        error=result.error,
    )
