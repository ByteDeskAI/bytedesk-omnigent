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
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.agent_refs import require_agent_ref
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.etag import parse_if_match
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes.builtin_agents import _to_agent_object
from omnigent.server.schemas import AgentObject
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.permission_store import PermissionStore

# Marker written to ``sot_tier`` once an agent is edited here, so the
# startup wheel re-seed (``_ensure_builtin_agent``) leaves it alone.
_MIGRATED_TIER = "migrated"
_MAX_TEXT_FILE_BYTES = 256 * 1024
_CACHE_MARKER_FILE = ".omnigent-bundle-location"


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


class AgentImageTreeEntry(BaseModel):
    """One entry in an agent image directory listing."""

    name: str
    path: str
    type: Literal["directory", "file"]
    size: int | None = None


class AgentImageTree(BaseModel):
    """Directory listing for an extracted template-agent image."""

    id: str
    name: str
    version: int
    path: str
    entries: list[AgentImageTreeEntry]


class AgentImageFile(BaseModel):
    """Text file read from an extracted template-agent image."""

    id: str
    name: str
    version: int
    path: str
    content: str
    size: int


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


def _safe_join_or_root(root: Path, relpath: str | None) -> Path:
    """Resolve an optional image-relative path, allowing the image root."""
    if relpath is None or relpath == "" or relpath == ".":
        return root.resolve()
    return _safe_join(root, relpath)


def _image_relpath(root: Path, path: Path) -> str:
    """Return *path* relative to image *root* as a forward-slash string."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def _list_image_tree(root: Path, relpath: str | None) -> tuple[str, list[AgentImageTreeEntry]]:
    """List the immediate children of an image directory."""
    directory = _safe_join_or_root(root, relpath)
    if not directory.exists():
        raise OmnigentError("image directory not found", code=ErrorCode.NOT_FOUND)
    if not directory.is_dir():
        raise OmnigentError("image path is not a directory", code=ErrorCode.INVALID_INPUT)

    entries: list[AgentImageTreeEntry] = []
    children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children:
        if child.name == _CACHE_MARKER_FILE:
            continue
        child_type: Literal["directory", "file"] = "directory" if child.is_dir() else "file"
        entries.append(
            AgentImageTreeEntry(
                name=child.name,
                path=_image_relpath(root, child),
                type=child_type,
                size=None if child.is_dir() else child.stat().st_size,
            )
        )
    return _image_relpath(root, directory), entries


def _read_image_text_file(root: Path, relpath: str) -> tuple[str, str, int]:
    """Read a bounded UTF-8 text file from an image."""
    target = _safe_join(root, relpath)
    if target.name == _CACHE_MARKER_FILE and target.parent.resolve() == root.resolve():
        raise OmnigentError("image file not found", code=ErrorCode.NOT_FOUND)
    if not target.exists():
        raise OmnigentError("image file not found", code=ErrorCode.NOT_FOUND)
    if not target.is_file():
        raise OmnigentError("image path is not a file", code=ErrorCode.INVALID_INPUT)

    size = target.stat().st_size
    if size > _MAX_TEXT_FILE_BYTES:
        raise OmnigentError(
            f"image file is too large to read inline ({size} bytes)",
            code=ErrorCode.INVALID_INPUT,
        )
    raw = target.read_bytes()
    if b"\x00" in raw:
        raise OmnigentError("image file is binary", code=ErrorCode.INVALID_INPUT)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OmnigentError("image file is not valid UTF-8", code=ErrorCode.INVALID_INPUT) from exc
    return _image_relpath(root, target), content, size


def _load_template_image(agent_cache: AgentCache, agent: Any):
    """Load a template image, mapping missing blob storage to 404."""
    try:
        return agent_cache.load(agent.id, agent.bundle_location, expand_env=False)
    except KeyError as exc:
        raise OmnigentError(
            f"Agent image bundle not found for {agent.id!r}",
            code=ErrorCode.NOT_FOUND,
        ) from exc


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
    permission_store: PermissionStore | None = None,
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

    async def _require_admin(request: Request) -> None:
        user_id = _require_user(request, auth_provider)
        if permission_store is None:
            return
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        if not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required to manage agent images",
                code=ErrorCode.FORBIDDEN,
            )

    @router.get("/agents/{agent_id}/image")
    async def get_agent_image(
        request: Request, response: Response, agent_id: str
    ) -> AgentImage:
        """Return the editable image of a template agent.

        Emits the agent ``version`` as a strong ``ETag`` header so an
        editor can round-trip it as ``If-Match`` on the PUT for optimistic
        concurrency (BDP-2412 / ADR-0150); the version is also in the body.

        :param request: Incoming request (for auth).
        :param response: Response (to set the ``ETag`` header).
        :param agent_id: Template agent id, e.g. ``"ag_abc123"``.
        :returns: The :class:`AgentImage` editable surface.
        :raises OmnigentError: If the agent is missing or session-scoped.
        """
        _require_user(request, auth_provider)
        agent = await asyncio.to_thread(
            require_agent_ref,
            agent_store,
            agent_id,
            template_only=True,
        )
        _require_template(agent, agent_id)
        response.headers["ETag"] = f'"{agent.version}"'

        # expand_env=False: we read the raw config.yaml / AGENTS.md files
        # off the extracted workdir, so ${VAR} references come back
        # verbatim (never resolved server secrets) for the editor.
        loaded = await asyncio.to_thread(_load_template_image, agent_cache, agent)
        surface = await asyncio.to_thread(_read_image, loaded.workdir)
        sot_tier = await asyncio.to_thread(agent_store.get_sot_tier, agent.id)
        return AgentImage(
            id=agent.id,
            name=agent.name,
            version=agent.version,
            sot_tier=sot_tier,
            **surface,
        )

    @router.get("/agents/{agent_id}/image/tree")
    async def get_agent_image_tree(
        request: Request,
        response: Response,
        agent_id: str,
        path: str = "",
    ) -> AgentImageTree:
        """Return a directory listing for a template agent image."""
        await _require_admin(request)
        agent = await asyncio.to_thread(
            require_agent_ref,
            agent_store,
            agent_id,
            template_only=True,
        )
        _require_template(agent, agent_id)
        response.headers["ETag"] = f'"{agent.version}"'
        loaded = await asyncio.to_thread(_load_template_image, agent_cache, agent)
        relpath, entries = await asyncio.to_thread(_list_image_tree, loaded.workdir, path)
        return AgentImageTree(
            id=agent.id,
            name=agent.name,
            version=agent.version,
            path=relpath,
            entries=entries,
        )

    @router.get("/agents/{agent_id}/image/file")
    async def get_agent_image_file(
        request: Request,
        response: Response,
        agent_id: str,
        path: str,
    ) -> AgentImageFile:
        """Return one bounded UTF-8 text file from a template agent image."""
        await _require_admin(request)
        agent = await asyncio.to_thread(
            require_agent_ref,
            agent_store,
            agent_id,
            template_only=True,
        )
        _require_template(agent, agent_id)
        response.headers["ETag"] = f'"{agent.version}"'
        loaded = await asyncio.to_thread(_load_template_image, agent_cache, agent)
        relpath, content, size = await asyncio.to_thread(
            _read_image_text_file, loaded.workdir, path
        )
        return AgentImageFile(
            id=agent.id,
            name=agent.name,
            version=agent.version,
            path=relpath,
            content=content,
            size=size,
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
        await _require_admin(request)
        agent = await asyncio.to_thread(
            require_agent_ref,
            agent_store,
            agent_id,
            template_only=True,
        )
        _require_template(agent, agent_id)
        # If-Match optimistic concurrency (BDP-2412): the agent version the
        # editor last read, threaded into the row write as a compare-and-swap
        # so two concurrent edits can't silently clobber each other.
        expected_version = parse_if_match(request.headers.get("if-match"))

        loaded = await asyncio.to_thread(_load_template_image, agent_cache, agent)

        def _rebuild(src_workdir: Path) -> bytes:
            # Copy the whole current image forward so unedited
            # assets (skills/tools/sub-agents/AGENTS.md) are preserved,
            # then overlay the supplied edits.
            staging = Path(tempfile.mkdtemp(prefix=f"{agent.id}_edit_"))
            try:
                shutil.copytree(src_workdir, staging, dirs_exist_ok=True)
                (staging / _CACHE_MARKER_FILE).unlink(missing_ok=True)
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
            expected_version=expected_version,
        )
        # Mark omnigent as config SoT so the boot re-seed leaves it
        # alone (idempotent; safe on a content no-op too).
        await asyncio.to_thread(agent_store.set_sot_tier, agent.id, _MIGRATED_TIER)
        # Materialize the declared capability surface (BDP-2334) so the
        # assignment resolver can read persisted capabilities back.
        await asyncio.to_thread(
            agent_store.set_capabilities, agent.id, spec.capabilities
        )

        return _to_agent_object(updated, agent_cache)

    return router
