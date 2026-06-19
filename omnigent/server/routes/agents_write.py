"""Write routes for template agents — read & rewrite the agent image.

``GET /v1/agents/{id}/image`` returns a template agent's editable
surface (the parsed ``config.yaml`` plus ``AGENTS.md`` instructions and
an inventory of bundled skills/tools/sub-agents). ``PUT
/v1/agents/{id}/image`` rewrites it: the current image is copied
forward, the supplied parts are overwritten, the bundle is rebuilt
(content-addressed), stored, and the cache is warm-swapped — so a config
change is live for new sessions with no server restart.

Scope: **template agents only** (``session_id IS NULL`` — the org-chart
personas, built-ins, ``--agent`` extras). Session-scoped agents are
edited through ``PUT /v1/sessions/{id}/agent`` and are rejected here.

The agent image *is* the spec, so this is the full "every property"
surface: everything in ``config.yaml`` (llm, interaction, tools, params,
executor, compaction, guardrails, async/timers/spawn, os_env, terminals,
skills_filter), the ``AGENTS.md`` instructions, and — via ``files`` —
arbitrary in-bundle files (MCP declarations, SKILL.md content, local
tools).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes.builtin_agents import _to_agent_object
from omnigent.server.schemas import AgentObject
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

# Marker written to ``sot_tier`` once an agent is edited here, so the
# startup wheel re-seed (``_ensure_builtin_agent``) leaves it alone.
_MIGRATED_TIER = "migrated"


class AgentImage(BaseModel):
    """The editable surface of a template agent's image (GET response)."""

    id: str
    name: str
    version: int
    config: dict[str, Any]
    instructions: str | None
    skills: list[str]
    mcp_servers: list[str]
    python_tools: list[str]
    typescript_tools: list[str]
    sub_agents: list[str]
    sot_tier: str | None


class AgentImageUpdate(BaseModel):
    """Partial edit to a template agent's image (PUT body).

    Only the supplied parts are overwritten; everything else (skills,
    tools, sub-agents) is preserved from the current bundle. ``config``,
    when present, replaces ``config.yaml`` wholesale — the editor holds
    the full document, so there is no server-side merge to disagree
    about.
    """

    config: dict[str, Any] | None = None
    instructions: str | None = None
    # Arbitrary in-bundle files to write/overwrite, keyed by
    # forward-slash relative path (e.g. ``"tools/mcp/jira.yaml"``,
    # ``"skills/deep-search/SKILL.md"``). Path-validated to stay inside
    # the image.
    files: dict[str, str] | None = None
    # In-bundle files to delete (same relative-path rules).
    remove: list[str] | None = None


def _safe_join(root: Path, relpath: str) -> Path:
    """
    Resolve *relpath* under *root*, rejecting traversal/escape.

    :param root: The image root directory.
    :param relpath: Forward-slash relative path supplied by the caller.
    :returns: The resolved absolute path, guaranteed within *root*.
    :raises OmnigentError: If the path is empty, absolute, contains
        ``..`` or backslashes, or resolves outside *root*.
    """
    parts = PurePosixPath(relpath).parts
    if (
        not parts
        or ".." in parts
        or "\\" in relpath
        or PurePosixPath(relpath).is_absolute()
        or PureWindowsPath(relpath).is_absolute()
    ):
        raise OmnigentError(f"invalid file path: {relpath!r}", code=ErrorCode.INVALID_INPUT)
    resolved = (root / Path(*parts)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise OmnigentError(
            f"file path escapes the agent image: {relpath!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return resolved


def _names_in(directory: Path, *, dirs: bool) -> list[str]:
    """List sorted child names of *directory* (dirs or files), or []."""
    if not directory.is_dir():
        return []
    out = [p.name for p in directory.iterdir() if (p.is_dir() if dirs else p.is_file())]
    return sorted(out)


def _read_image(workdir: Path) -> dict[str, Any]:
    """Read the editable surface from an extracted image *workdir*."""
    config_path = workdir / "config.yaml"
    config: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if isinstance(loaded, dict):
            config = loaded
    agents_md = workdir / "AGENTS.md"
    instructions = agents_md.read_text() if agents_md.is_file() else None
    tools = workdir / "tools"
    return {
        "config": config,
        "instructions": instructions,
        "skills": _names_in(workdir / "skills", dirs=True),
        "mcp_servers": [p.stem for p in sorted((tools / "mcp").glob("*.yaml"))]
        if (tools / "mcp").is_dir()
        else [],
        "python_tools": [p.stem for p in sorted((tools / "python").glob("*.py"))]
        if (tools / "python").is_dir()
        else [],
        "typescript_tools": [p.stem for p in sorted((tools / "typescript").glob("*.ts"))]
        if (tools / "typescript").is_dir()
        else [],
        "sub_agents": _names_in(workdir / "agents", dirs=True),
    }


def _require_template(agent: Any, agent_id: str) -> None:
    """404 if missing; reject session-scoped agents (template-only route)."""
    if agent is None:
        raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
    if agent.session_id is not None:
        raise OmnigentError(
            "image editing is only available for template agents; "
            "session-scoped agents are edited via PUT /v1/sessions/{id}/agent",
            code=ErrorCode.INVALID_INPUT,
        )


def create_agents_write_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    artifact_store: ArtifactStore | None,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for template-agent image read/write.

    Mounted with ``prefix="/v1"`` so the final paths are
    ``/v1/agents/{id}/image``.

    :param agent_store: Store for agent metadata.
    :param agent_cache: Two-tier cache (gives the extracted workdir and
        the warm-swap primitive).
    :param artifact_store: Blob store for bundle bytes.
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :returns: A FastAPI router exposing GET/PUT ``/agents/{id}/image``.
    """
    router = APIRouter()

    @router.get("/agents/{agent_id}/image")
    async def get_agent_image(request: Request, agent_id: str) -> AgentImage:
        """Return the editable image of a template agent.

        :param request: Incoming request (for auth).
        :param agent_id: Template agent id, e.g. ``"ag_abc123"``.
        :returns: The :class:`AgentImage` editable surface.
        :raises OmnigentError: If the agent is missing or session-scoped.
        """
        _require_user(request, auth_provider)
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        _require_template(agent, agent_id)

        # expand_env=False: we read the raw config.yaml / AGENTS.md files
        # off the extracted workdir, so ${VAR} references come back
        # verbatim (never resolved server secrets) for the editor.
        loaded = await asyncio.to_thread(
            agent_cache.load, agent.id, agent.bundle_location, expand_env=False
        )
        surface = await asyncio.to_thread(_read_image, loaded.workdir)
        sot_tier = await asyncio.to_thread(agent_store.get_sot_tier, agent.id)
        return AgentImage(
            id=agent.id,
            name=agent.name,
            version=agent.version,
            sot_tier=sot_tier,
            **surface,
        )

    @router.put("/agents/{agent_id}/image")
    async def put_agent_image(
        request: Request,
        agent_id: str,
        body: AgentImageUpdate,
    ) -> AgentObject:
        """Rewrite a template agent's image; live without a restart.

        Copies the current image forward, overwrites the supplied parts
        (``config`` replaces ``config.yaml``; ``instructions`` writes
        ``AGENTS.md``; ``files``/``remove`` edit arbitrary in-bundle
        files), rebuilds + content-addresses the bundle, stores it,
        warm-swaps the cache, and marks the agent ``sot_tier=migrated``
        so the startup wheel re-seed won't clobber the edit.

        :param request: Incoming request (for auth).
        :param agent_id: Template agent id.
        :param body: The partial image edit.
        :returns: The updated :class:`AgentObject`.
        :raises OmnigentError: If the agent is missing/session-scoped,
            the rebuilt bundle is invalid, or the spec name changed
            (name is immutable).
        """
        _require_user(request, auth_provider)
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        _require_template(agent, agent_id)

        loaded = await asyncio.to_thread(
            agent_cache.load, agent.id, agent.bundle_location, expand_env=False
        )

        def _rebuild(src_workdir: Path) -> bytes:
            # Copy the whole current image forward so unedited
            # assets (skills/tools/sub-agents/AGENTS.md) are preserved,
            # then overlay the supplied edits.
            staging = Path(tempfile.mkdtemp(prefix=f"{agent.id}_edit_"))
            try:
                shutil.copytree(src_workdir, staging, dirs_exist_ok=True)
                if body.config is not None:
                    (staging / "config.yaml").write_text(
                        yaml.safe_dump(body.config, sort_keys=False)
                    )
                if body.instructions is not None:
                    (staging / "AGENTS.md").write_text(body.instructions)
                for relpath, content in (body.files or {}).items():
                    target = _safe_join(staging, relpath)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                for relpath in body.remove or []:
                    target = _safe_join(staging, relpath)
                    if target.is_file():
                        target.unlink()
                return build_bundle_bytes(staging)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        bundle_bytes = await asyncio.to_thread(_rebuild, loaded.workdir)

        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name is None:
            raise OmnigentError("spec missing name", code=ErrorCode.INVALID_INPUT)
        if spec.name != agent.name:
            raise OmnigentError(
                f"spec name '{spec.name}' does not match agent "
                f"name '{agent.name}'; name is immutable",
                code=ErrorCode.INVALID_INPUT,
            )

        # Template agents are operator-authored, so ${VAR} expands
        # against the server env (expand_env=True).
        updated = await asyncio.to_thread(
            apply_bundle_update,
            agent,
            bundle_bytes,
            artifact_store=artifact_store,
            agent_store=agent_store,
            agent_cache=agent_cache,
            expand_env=True,
        )
        # Mark omnigent as config SoT so the boot re-seed leaves it
        # alone (idempotent; safe on a content no-op too).
        await asyncio.to_thread(agent_store.set_sot_tier, agent.id, _MIGRATED_TIER)

        return _to_agent_object(updated, agent_cache)

    return router
