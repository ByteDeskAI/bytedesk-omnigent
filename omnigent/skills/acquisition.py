"""Discover, stage, preview, and apply agent skill packages.

The framework is deliberately split into non-mutating stages and a final
apply stage:

1. Source adapters run in a temporary workspace and discover skill folders.
2. A preview records immutable package files plus per-agent actions.
3. Apply copies approved packages into template-agent bundles through the
   existing bundle validation and persistence path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

import yaml

from omnigent.errors import ErrorCode, OmnigentError, StaleWriteError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.auth import local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.routes.agents_write import _MIGRATED_TIER, _require_template
from omnigent.server.routes.builtin_agents import _to_agent_object
from omnigent.spec.tar_utils import ExtractionError, build_bundle_bytes, extract_safe
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

SkillOperation = Literal["install", "remove"]
InstallMode = Literal["replace", "skip_existing", "fail_on_existing"]
PreviewAction = Literal["install", "replace", "skip", "remove", "missing", "conflict"]
ApplyStatus = Literal["applied", "skipped", "failed"]

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)", re.DOTALL)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_DEFAULT_TTL_SECONDS = 60 * 60
_MAX_SKILL_BYTES = 50 * 1024 * 1024
_MAX_SKILL_FILES = 1_000
_MAX_COMMAND_OUTPUT_CHARS = 64 * 1024


@dataclass(frozen=True)
class SkillCommandSpec:
    """A command source to run inside an isolated staging workspace."""

    argv: tuple[str, ...] | None = None
    shell: str | None = None
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if bool(self.argv) == bool(self.shell):
            raise OmnigentError(
                "exactly one of command.argv or command.shell is required",
                code=ErrorCode.INVALID_INPUT,
            )
        if self.argv is not None and len(self.argv) == 0:
            raise OmnigentError("command.argv cannot be empty", code=ErrorCode.INVALID_INPUT)
        if self.timeout_seconds < 1 or self.timeout_seconds > 600:
            raise OmnigentError(
                "command.timeout_seconds must be between 1 and 600",
                code=ErrorCode.INVALID_INPUT,
            )


@dataclass(frozen=True)
class CommandEvidence:
    """Captured command execution evidence for a staged package."""

    command: list[str] | str
    shell: bool
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SkillFileManifest:
    """One file staged for installation."""

    path: str
    size: int
    sha256: str
    binary: bool


@dataclass(frozen=True)
class StagedSkillPackage:
    """A validated skill directory staged for later application."""

    name: str
    description: str
    root: Path
    files: tuple[SkillFileManifest, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass(frozen=True)
class SkillTargetAction:
    """A previewed action for one target agent and one skill."""

    agent_id: str
    agent_name: str
    agent_version: int
    skill_name: str
    action: PreviewAction
    reason: str | None = None


@dataclass
class SkillPreview:
    """Immutable preview state retained until apply or expiry."""

    id: str
    operation: SkillOperation
    install_mode: InstallMode
    packages: tuple[StagedSkillPackage, ...]
    target_actions: tuple[SkillTargetAction, ...]
    created_at: int
    expires_at: int
    command: CommandEvidence | None = None
    skill_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SkillApplyResult:
    """Apply result for one agent."""

    agent_id: str
    status: ApplyStatus
    action_count: int = 0
    version: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class SkillSearchHit:
    """A source discovery result that can be sent back to preview."""

    source: str
    name: str
    description: str | None = None
    source_ref: str | None = None
    version: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class SkillSearchOutcome:
    """Search results plus per-source errors."""

    results: tuple[SkillSearchHit, ...]
    errors: tuple[str, ...] = ()


class SkillCommandRunner:
    """Runs source commands in a staging workspace."""

    def run(self, spec: SkillCommandSpec, cwd: Path) -> CommandEvidence:
        """Execute *spec* in *cwd* and capture bounded evidence."""
        cwd.mkdir(parents=True, exist_ok=True)
        home = cwd / ".home"
        home.mkdir(exist_ok=True)
        env = _command_env(home, cwd)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(spec.argv) if spec.argv is not None else spec.shell,
                cwd=cwd,
                env=env,
                shell=spec.shell is not None,
                text=False,
                capture_output=True,
                timeout=spec.timeout_seconds,
                check=False,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            return CommandEvidence(
                command=list(spec.argv) if spec.argv is not None else str(spec.shell),
                shell=spec.shell is not None,
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                stdout=_decode_output(completed.stdout),
                stderr=_decode_output(completed.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return CommandEvidence(
                command=list(spec.argv) if spec.argv is not None else str(spec.shell),
                shell=spec.shell is not None,
                exit_code=124,
                duration_ms=duration_ms,
                stdout=_decode_output(exc.stdout or b""),
                stderr=_decode_output(exc.stderr or b"") + "\ncommand timed out",
            )


class SkillAcquisitionService:
    """Framework service for source adapters, previews, and applies."""

    def __init__(
        self,
        *,
        agent_store: AgentStore,
        agent_cache: AgentCache,
        artifact_store: ArtifactStore | None,
        runner: SkillCommandRunner | None = None,
        stage_root: Path | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._agent_store = agent_store
        self._agent_cache = agent_cache
        self._artifact_store = artifact_store
        self._runner = runner or SkillCommandRunner()
        self._stage_root = stage_root or Path(tempfile.mkdtemp(prefix="omnigent_skills_"))
        self._ttl_seconds = ttl_seconds
        self._previews: dict[str, SkillPreview] = {}
        self._previews_lock = threading.RLock()

    def sources(self) -> list[dict[str, object]]:
        """Return the source adapters available to the framework."""
        return [
            {
                "id": "skills",
                "label": "Agent Skills CLI",
                "kind": "named_adapter",
                "supports_search": True,
                "supports_preview": True,
                "high_risk": False,
            },
            {
                "id": "npm",
                "label": "npm Packages",
                "kind": "named_adapter",
                "supports_search": True,
                "supports_preview": True,
                "high_risk": False,
            },
            {
                "id": "github",
                "label": "GitHub / Git",
                "kind": "named_adapter",
                "supports_search": False,
                "supports_preview": True,
                "high_risk": False,
            },
            {
                "id": "skills-npm",
                "label": "skills-npm",
                "kind": "named_adapter",
                "supports_search": False,
                "supports_preview": True,
                "high_risk": False,
            },
            {
                "id": "configured",
                "label": "Configured Command",
                "kind": "command_template",
                "supports_search": False,
                "supports_preview": True,
                "high_risk": True,
            },
            {
                "id": "freeform",
                "label": "Free-form Command",
                "kind": "freeform_command",
                "supports_search": False,
                "supports_preview": True,
                "high_risk": True,
            },
        ]

    def search(
        self,
        query: str,
        *,
        sources: list[str] | None = None,
        limit: int = 20,
        command: SkillCommandSpec | None = None,
    ) -> SkillSearchOutcome:
        """Search configured sources for skills."""
        selected = sources or ["skills", "npm"]
        hits: list[SkillSearchHit] = []
        errors: list[str] = []
        for source in selected:
            try:
                if source == "skills":
                    hits.extend(self._search_skills_cli(query, limit=limit))
                elif source == "npm":
                    hits.extend(self._search_npm(query, limit=limit))
                elif source in {"freeform", "configured"} and command is not None:
                    hits.extend(self._search_freeform(query, command, source=source, limit=limit))
                else:
                    errors.append(f"{source}: search is not supported")
            except OmnigentError as exc:
                errors.append(f"{source}: {exc.message}")
        return SkillSearchOutcome(results=tuple(hits[:limit]), errors=tuple(errors))

    def installed(self, *, agent_id: str | None = None) -> list[dict[str, object]]:
        """Return installed bundled skills across template agents."""
        if agent_id is not None:
            agent = self._agent_store.get(agent_id)
            _require_template(agent, agent_id)
            assert agent is not None
            agents = [agent]
        else:
            agents = self._agent_store.list(limit=1000, order="asc").data
        by_skill: dict[str, dict[str, object]] = {}
        for agent in agents:
            _require_template(agent, agent.id)
            loaded = self._agent_cache.load(
                agent.id,
                agent.bundle_location,
                expand_env=False,
            )
            for skill in loaded.spec.skills:
                row = by_skill.setdefault(
                    skill.name,
                    {"name": skill.name, "description": skill.description, "agents": []},
                )
                agents_list = row["agents"]
                assert isinstance(agents_list, list)
                agents_list.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "version": agent.version,
                    }
                )
        return sorted(by_skill.values(), key=lambda item: str(item["name"]))

    def create_preview(
        self,
        *,
        operation: SkillOperation,
        target_agent_ids: list[str],
        install_mode: InstallMode = "replace",
        source: str = "freeform",
        source_ref: str | None = None,
        command: SkillCommandSpec | None = None,
        selected_skill_names: list[str] | None = None,
        skill_names: list[str] | None = None,
    ) -> SkillPreview:
        """Create a non-mutating preview for install or removal."""
        self._gc()
        if not target_agent_ids:
            raise OmnigentError(
                "target_agent_ids is required",
                code=ErrorCode.INVALID_INPUT,
            )
        preview_id = f"skprev_{uuid4().hex}"
        preview_dir = self._stage_root / preview_id
        workspace = preview_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        command_evidence: CommandEvidence | None = None
        packages: tuple[StagedSkillPackage, ...] = ()
        names: tuple[str, ...]

        if operation == "install":
            command_evidence = self._resolve_source(
                source=source,
                source_ref=source_ref,
                command=command,
                workspace=workspace,
                selected_skill_names=selected_skill_names or [],
            )
            if command_evidence is not None and command_evidence.exit_code != 0:
                raise OmnigentError(
                    f"skill source command failed with exit code {command_evidence.exit_code}: "
                    f"{command_evidence.stderr.strip()}",
                    code=ErrorCode.INVALID_INPUT,
                )
            packages = tuple(discover_skill_packages(workspace))
            if selected_skill_names:
                selected = set(selected_skill_names)
                packages = tuple(pkg for pkg in packages if pkg.name in selected)
            if not packages:
                raise OmnigentError(
                    "no valid skill directories were discovered",
                    code=ErrorCode.INVALID_INPUT,
                )
            names = tuple(pkg.name for pkg in packages)
        else:
            if not skill_names:
                raise OmnigentError(
                    "skill_names is required for removal previews",
                    code=ErrorCode.INVALID_INPUT,
                )
            names = tuple(sorted(set(skill_names)))

        actions = self._build_target_actions(
            operation=operation,
            target_agent_ids=target_agent_ids,
            skill_names=names,
            install_mode=install_mode,
        )
        now = int(time.time())
        preview = SkillPreview(
            id=preview_id,
            operation=operation,
            install_mode=install_mode,
            packages=packages,
            target_actions=tuple(actions),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            command=command_evidence,
            skill_names=names,
        )
        with self._previews_lock:
            self._previews[preview_id] = preview
        return preview

    def get_preview(self, preview_id: str) -> SkillPreview:
        """Return a retained preview or raise 404/410-like errors."""
        self._gc()
        with self._previews_lock:
            preview = self._previews.get(preview_id)
        if preview is None:
            raise OmnigentError(
                f"skill preview not found: {preview_id}",
                code=ErrorCode.NOT_FOUND,
            )
        return preview

    def apply_preview(
        self,
        preview_id: str,
        *,
        agent_ids: list[str] | None = None,
    ) -> list[SkillApplyResult]:
        """Apply an existing preview to its target agents."""
        preview = self.get_preview(preview_id)
        selected_ids = set(agent_ids) if agent_ids else {
            action.agent_id for action in preview.target_actions
        }
        results: list[SkillApplyResult] = []
        actions_by_agent: dict[str, list[SkillTargetAction]] = {}
        for action in preview.target_actions:
            if action.agent_id in selected_ids:
                actions_by_agent.setdefault(action.agent_id, []).append(action)
        packages_by_name = {pkg.name: pkg for pkg in preview.packages}

        for agent_id, actions in actions_by_agent.items():
            try:
                if any(action.action == "conflict" for action in actions):
                    results.append(
                        SkillApplyResult(
                            agent_id=agent_id,
                            status="failed",
                            error="preview has unresolved conflicts",
                        )
                    )
                    continue
                effective = [a for a in actions if a.action not in {"skip", "missing"}]
                if not effective:
                    agent = self._agent_store.get(agent_id)
                    results.append(
                        SkillApplyResult(
                            agent_id=agent_id,
                            status="skipped",
                            version=agent.version if agent is not None else None,
                        )
                    )
                    continue
                agent = self._agent_store.get(agent_id)
                _require_template(agent, agent_id)
                assert agent is not None
                expected_version = actions[0].agent_version
                updated = self._apply_agent_actions(
                    agent=agent,
                    actions=effective,
                    packages_by_name=packages_by_name,
                    expected_version=expected_version,
                )
                results.append(
                    SkillApplyResult(
                        agent_id=agent_id,
                        status="applied",
                        action_count=len(effective),
                        version=updated.version,
                    )
                )
            except StaleWriteError as exc:
                results.append(
                    SkillApplyResult(agent_id=agent_id, status="failed", error=exc.message)
                )
            except OmnigentError as exc:
                results.append(
                    SkillApplyResult(agent_id=agent_id, status="failed", error=exc.message)
                )
        return results

    def _search_skills_cli(self, query: str, *, limit: int) -> list[SkillSearchHit]:
        workspace = self._ephemeral_workspace()
        evidence = self._runner.run(
            SkillCommandSpec(argv=("npx", "-y", "skills", "find", query), timeout_seconds=30),
            workspace,
        )
        if evidence.exit_code != 0:
            raise OmnigentError(
                evidence.stderr.strip() or "skills find failed",
                code=ErrorCode.INVALID_INPUT,
            )
        return _parse_line_search("skills", evidence.stdout, limit=limit)

    def _search_npm(self, query: str, *, limit: int) -> list[SkillSearchHit]:
        workspace = self._ephemeral_workspace()
        evidence = self._runner.run(
            SkillCommandSpec(
                argv=("npm", "search", query, "--json", "--searchlimit", str(limit)),
                timeout_seconds=30,
            ),
            workspace,
        )
        if evidence.exit_code != 0:
            raise OmnigentError(
                evidence.stderr.strip() or "npm search failed",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            raw = json.loads(evidence.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"npm search returned invalid JSON: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        hits: list[SkillSearchHit] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            links = item.get("links") if isinstance(item.get("links"), dict) else {}
            hits.append(
                SkillSearchHit(
                    source="npm",
                    name=str(name),
                    description=str(item.get("description")) if item.get("description") else None,
                    source_ref=str(name),
                    version=str(item.get("version")) if item.get("version") else None,
                    url=str(links.get("npm") or links.get("repository") or "") or None,
                )
            )
        return hits

    def _search_freeform(
        self,
        query: str,
        command: SkillCommandSpec,
        *,
        source: str,
        limit: int,
    ) -> list[SkillSearchHit]:
        workspace = self._ephemeral_workspace()
        evidence = self._runner.run(command, workspace)
        if evidence.exit_code != 0:
            raise OmnigentError(
                evidence.stderr.strip() or "search command failed",
                code=ErrorCode.INVALID_INPUT,
            )
        hits = _parse_line_search(source, evidence.stdout, limit=limit)
        query_lower = query.lower()
        return [
            hit
            for hit in hits
            if query_lower in (hit.name + " " + (hit.description or "")).lower()
        ][:limit]

    def _resolve_source(
        self,
        *,
        source: str,
        source_ref: str | None,
        command: SkillCommandSpec | None,
        workspace: Path,
        selected_skill_names: list[str],
    ) -> CommandEvidence | None:
        if source in {"freeform", "configured"}:
            if command is None:
                raise OmnigentError(
                    f"{source} preview requires command",
                    code=ErrorCode.INVALID_INPUT,
                )
            return self._runner.run(command, workspace)
        if source == "skills":
            if not source_ref:
                raise OmnigentError(
                    "skills preview requires source_ref",
                    code=ErrorCode.INVALID_INPUT,
                )
            argv: list[str] = [
                "npx",
                "-y",
                "skills",
                "add",
                source_ref,
                "--copy",
                "--yes",
                "--agent",
                "codex",
            ]
            if selected_skill_names:
                for name in selected_skill_names:
                    argv.extend(["--skill", name])
            else:
                argv.append("--all")
            return self._runner.run(
                SkillCommandSpec(argv=tuple(argv), timeout_seconds=120),
                workspace,
            )
        if source == "skills-npm":
            if not source_ref:
                raise OmnigentError(
                    "skills-npm preview requires source_ref",
                    code=ErrorCode.INVALID_INPUT,
                )
            return self._runner.run(
                SkillCommandSpec(
                    argv=("npx", "-y", "skills-npm", source_ref),
                    timeout_seconds=120,
                ),
                workspace,
            )
        if source == "github":
            if not source_ref:
                raise OmnigentError(
                    "github preview requires source_ref",
                    code=ErrorCode.INVALID_INPUT,
                )
            return self._runner.run(
                SkillCommandSpec(
                    argv=("git", "clone", "--depth=1", source_ref, "checkout"),
                    timeout_seconds=120,
                ),
                workspace,
            )
        if source == "npm":
            if not source_ref:
                raise OmnigentError(
                    "npm preview requires source_ref",
                    code=ErrorCode.INVALID_INPUT,
                )
            evidence = self._runner.run(
                SkillCommandSpec(
                    argv=(
                        "npm",
                        "pack",
                        source_ref,
                        "--json",
                        "--pack-destination",
                        str(workspace),
                    ),
                    timeout_seconds=120,
                ),
                workspace,
            )
            if evidence.exit_code == 0:
                _extract_npm_tgz(workspace)
            return evidence
        raise OmnigentError(f"unknown skill source: {source}", code=ErrorCode.INVALID_INPUT)

    def _build_target_actions(
        self,
        *,
        operation: SkillOperation,
        target_agent_ids: list[str],
        skill_names: tuple[str, ...],
        install_mode: InstallMode,
    ) -> list[SkillTargetAction]:
        actions: list[SkillTargetAction] = []
        for agent_id in target_agent_ids:
            agent = self._agent_store.get(agent_id)
            _require_template(agent, agent_id)
            assert agent is not None
            loaded = self._agent_cache.load(
                agent.id,
                agent.bundle_location,
                expand_env=False,
            )
            existing = {skill.name for skill in loaded.spec.skills}
            for name in skill_names:
                if operation == "remove":
                    action: PreviewAction = "remove" if name in existing else "missing"
                    reason = None if name in existing else "skill is not installed"
                elif name not in existing:
                    action = "install"
                    reason = None
                elif install_mode == "skip_existing":
                    action = "skip"
                    reason = "skill already installed"
                elif install_mode == "fail_on_existing":
                    action = "conflict"
                    reason = "skill already installed"
                else:
                    action = "replace"
                    reason = "skill already installed"
                actions.append(
                    SkillTargetAction(
                        agent_id=agent.id,
                        agent_name=agent.name,
                        agent_version=agent.version,
                        skill_name=name,
                        action=action,
                        reason=reason,
                    )
                )
        return actions

    def _apply_agent_actions(
        self,
        *,
        agent,
        actions: list[SkillTargetAction],
        packages_by_name: dict[str, StagedSkillPackage],
        expected_version: int,
    ):
        loaded = self._agent_cache.load(agent.id, agent.bundle_location, expand_env=False)
        staging = Path(tempfile.mkdtemp(prefix=f"{agent.id}_skills_"))
        try:
            shutil.copytree(loaded.workdir, staging, dirs_exist_ok=True)
            skills_root = staging / "skills"
            skills_root.mkdir(exist_ok=True)
            for action in actions:
                target = skills_root / action.skill_name
                if action.action == "remove":
                    shutil.rmtree(target, ignore_errors=True)
                    continue
                package = packages_by_name[action.skill_name]
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(package.root, target)
            bundle_bytes = build_bundle_bytes(staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        spec = validate_agent_bundle(
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name is None:
            raise OmnigentError("spec missing name", code=ErrorCode.INVALID_INPUT)
        if spec.name != agent.name:
            raise OmnigentError(
                f"spec name '{spec.name}' does not match agent name '{agent.name}'",
                code=ErrorCode.INVALID_INPUT,
            )
        updated = apply_bundle_update(
            agent,
            bundle_bytes,
            artifact_store=self._artifact_store,
            agent_store=self._agent_store,
            agent_cache=self._agent_cache,
            expand_env=True,
            expected_version=expected_version,
        )
        self._agent_store.set_sot_tier(agent.id, _MIGRATED_TIER)
        self._agent_store.set_capabilities(agent.id, spec.capabilities)
        return _to_agent_object(updated, self._agent_cache)

    def _ephemeral_workspace(self) -> Path:
        path = self._stage_root / f"search_{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _gc(self) -> None:
        now = int(time.time())
        with self._previews_lock:
            expired = [
                preview_id
                for preview_id, preview in self._previews.items()
                if preview.expires_at <= now
            ]
            for preview_id in expired:
                self._previews.pop(preview_id, None)
        for preview_id in expired:
            shutil.rmtree(self._stage_root / preview_id, ignore_errors=True)


def discover_skill_packages(root: Path) -> list[StagedSkillPackage]:
    """Find and validate skill directories under *root*."""
    skill_files = sorted(root.rglob("SKILL.md"))
    packages: list[StagedSkillPackage] = []
    seen_names: set[str] = set()
    seen_roots: list[Path] = []
    for skill_md in skill_files:
        skill_root = skill_md.parent
        if any(_is_relative_to(skill_root, existing) for existing in seen_roots):
            continue
        name, description = _parse_skill_frontmatter(skill_md)
        if name != skill_root.name:
            raise OmnigentError(
                f"skill name {name!r} must match directory {skill_root.name!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if name in seen_names:
            raise OmnigentError(
                f"duplicate staged skill name: {name}",
                code=ErrorCode.INVALID_INPUT,
            )
        files = tuple(_manifest_skill_files(skill_root, skill_name=name))
        packages.append(
            StagedSkillPackage(
                name=name,
                description=description,
                root=skill_root,
                files=files,
            )
        )
        seen_names.add(name)
        seen_roots.append(skill_root)
    return packages


def _parse_skill_frontmatter(skill_md: Path) -> tuple[str, str]:
    try:
        text = skill_md.read_text()
    except UnicodeDecodeError as exc:
        raise OmnigentError(
            f"SKILL.md must be UTF-8 text: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise OmnigentError(
            f"SKILL.md missing YAML frontmatter: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_frontmatter, _content = match.groups()
    try:
        frontmatter = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise OmnigentError(
            f"SKILL.md has invalid YAML frontmatter: {skill_md}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if not isinstance(frontmatter, dict):
        raise OmnigentError(
            f"SKILL.md frontmatter must be a mapping: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'name': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not _SKILL_NAME_RE.match(name) or len(name) > 64:
        raise OmnigentError(f"invalid skill name: {name!r}", code=ErrorCode.INVALID_INPUT)
    if not isinstance(description, str) or not description:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'description': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    if len(description) > 1024:
        raise OmnigentError(f"skill description is too long: {name}", code=ErrorCode.INVALID_INPUT)
    return name, description


def _manifest_skill_files(skill_root: Path, *, skill_name: str) -> list[SkillFileManifest]:
    manifests: list[SkillFileManifest] = []
    total_bytes = 0
    for path in sorted(skill_root.rglob("*")):
        if path.is_symlink():
            raise OmnigentError(f"skill contains a link: {path}", code=ErrorCode.INVALID_INPUT)
        if path.is_dir():
            continue
        if not path.is_file():
            raise OmnigentError(
                f"skill contains an unsupported entry type: {path}",
                code=ErrorCode.INVALID_INPUT,
            )
        rel = path.relative_to(skill_root).as_posix()
        if PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
            raise OmnigentError(f"invalid skill file path: {rel}", code=ErrorCode.INVALID_INPUT)
        data = path.read_bytes()
        total_bytes += len(data)
        if len(manifests) >= _MAX_SKILL_FILES:
            raise OmnigentError(
                f"skill {skill_name!r} exceeds max file count ({_MAX_SKILL_FILES})",
                code=ErrorCode.INVALID_INPUT,
            )
        if total_bytes > _MAX_SKILL_BYTES:
            raise OmnigentError(
                f"skill {skill_name!r} exceeds max size ({_MAX_SKILL_BYTES} bytes)",
                code=ErrorCode.INVALID_INPUT,
            )
        manifests.append(
            SkillFileManifest(
                path=f"skills/{skill_name}/{rel}",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                binary=_looks_binary(data),
            )
        )
    return manifests


def _command_env(home: Path, workspace: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "USER", "USERNAME", "SystemRoot", "COMSPEC"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["HOME"] = str(home)
    env["TMPDIR"] = str(workspace / ".tmp")
    env["TEMP"] = env["TMPDIR"]
    env["TMP"] = env["TMPDIR"]
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    env["DO_NOT_TRACK"] = "1"
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return env


def _decode_output(data: bytes | str) -> str:
    if isinstance(data, str):
        text = data
    else:
        text = data.decode("utf-8", errors="replace")
    if len(text) > _MAX_COMMAND_OUTPUT_CHARS:
        return text[:_MAX_COMMAND_OUTPUT_CHARS] + "\n[truncated]"
    return text


def _looks_binary(data: bytes) -> bool:
    sample = data[:4096]
    if b"\0" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _parse_line_search(source: str, text: str, *, limit: int) -> list[SkillSearchHit]:
    hits: list[SkillSearchHit] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        name, _, desc = line.partition(" - ")
        if not name:
            continue
        hits.append(
            SkillSearchHit(
                source=source,
                name=name.strip(),
                description=desc.strip() or None,
                source_ref=name.strip(),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _extract_npm_tgz(workspace: Path) -> None:
    for tgz in workspace.glob("*.tgz"):
        dest = workspace / "unpacked" / tgz.stem
        try:
            extract_safe(tgz, dest, max_bytes=_MAX_SKILL_BYTES * 2)
        except (ExtractionError, tarfile.TarError) as exc:
            raise OmnigentError(
                f"failed to extract npm package {tgz.name}: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
