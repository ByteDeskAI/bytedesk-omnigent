"""Unit tests for the skill acquisition framework."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.skills import acquisition as acq
from omnigent.skills.acquisition import (
    CommandEvidence,
    SkillAcquisitionService,
    SkillCommandRunner,
    SkillCommandSpec,
    _parse_skills_cli_search,
    discover_skill_packages,
)


def _write_skill(root: Path, name: str = "image-tools") -> Path:
    skill = root / name
    (skill / "assets").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Work with images.\n---\nUse this skill.\n"
    )
    (skill / "assets" / "icon.bin").write_bytes(b"\x00\x01binary")
    return skill


_SKILLS_FIND_STDOUT = (
    "\n"
    "\x1b[38;5;81m███████╗██╗  ██╗██╗██╗     ██╗     ███████╗\x1b[0m\n"
    "\x1b[38;5;81m██╔════╝██║ ██╔╝██║██║     ██║     ██╔════╝\x1b[0m\n"
    "\n"
    "\x1b[1mSearch results\x1b[0m\n"
    "19\n"
    "\n"
    "\x1b[38;5;102mInstall with\x1b[0m npx skills add <owner/repo@skill>\n"
    "\n"
    "\x1b[38;5;145mcoreyhaines31/marketingskills@seo-audit\x1b[0m "
    "\x1b[36m145.5K installs\x1b[0m\n"
    "\x1b[38;5;102m└ https://skills.sh/coreyhaines31/marketingskills/seo-audit\x1b[0m\n"
    "\n"
    "\x1b[38;5;145mresciencelab/opc-skills@seo-geo\x1b[0m \x1b[36m31.7K installs\x1b[0m\n"
    "\x1b[38;5;102m└ https://skills.sh/resciencelab/opc-skills/seo-geo\x1b[0m\n"
)


def test_parse_skills_cli_search_drops_banner_and_extracts_refs() -> None:
    hits = _parse_skills_cli_search(_SKILLS_FIND_STDOUT, limit=20)

    assert [h.name for h in hits] == [
        "coreyhaines31/marketingskills@seo-audit",
        "resciencelab/opc-skills@seo-geo",
    ]
    first = hits[0]
    assert first.source == "skills"
    assert first.source_ref == "coreyhaines31/marketingskills@seo-audit"
    assert first.description == "145.5K installs"
    assert first.url == "https://skills.sh/coreyhaines31/marketingskills/seo-audit"
    # Banner art, the "Search results"/count header, the "Install with ..."
    # footer, and the "└ https://..." URL lines must never become results.
    assert all("█" not in h.name and "Install" not in h.name for h in hits)


def test_parse_skills_cli_search_respects_limit() -> None:
    hits = _parse_skills_cli_search(_SKILLS_FIND_STDOUT, limit=1)

    assert [h.name for h in hits] == ["coreyhaines31/marketingskills@seo-audit"]


def test_discover_skill_packages_keeps_full_directory_manifest(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills")

    packages = discover_skill_packages(tmp_path)

    assert [p.name for p in packages] == ["image-tools"]
    paths = {f.path: f for f in packages[0].files}
    assert "skills/image-tools/SKILL.md" in paths
    assert paths["skills/image-tools/assets/icon.bin"].binary is True


def test_discover_skill_packages_rejects_name_directory_mismatch(tmp_path: Path) -> None:
    _write_skill(tmp_path / "skills", name="actual-name")
    (tmp_path / "skills" / "actual-name").rename(tmp_path / "skills" / "wrong-name")

    with pytest.raises(OmnigentError, match="must match directory"):
        discover_skill_packages(tmp_path)


def test_discover_skill_packages_rejects_links(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path / "skills")
    (skill / "escape").symlink_to(tmp_path)

    with pytest.raises(OmnigentError, match="contains a link"):
        discover_skill_packages(tmp_path)


def test_command_runner_returns_evidence_for_missing_argv_command(tmp_path: Path) -> None:
    runner = SkillCommandRunner()

    evidence = runner.run(
        SkillCommandSpec(argv=("omnigent-definitely-missing-command",), timeout_seconds=1),
        tmp_path,
    )

    assert evidence.exit_code == 127
    assert "command not found: omnigent-definitely-missing-command" in evidence.stderr


def test_sources_report_unavailable_named_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.skills.acquisition.shutil.which", lambda _cmd: None)
    service = SkillAcquisitionService(
        agent_store=None,  # type: ignore[arg-type]
        agent_cache=None,  # type: ignore[arg-type]
        artifact_store=None,
        stage_root=tmp_path,
    )

    sources = {source["id"]: source for source in service.sources()}

    assert sources["skills"]["available"] is False
    assert "npx" in str(sources["skills"]["unavailable_reason"])
    assert sources["github"]["available"] is False
    assert "gh" in str(sources["github"]["unavailable_reason"])
    assert sources["freeform"]["available"] is True


def test_search_returns_source_error_for_unavailable_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.skills.acquisition.shutil.which", lambda _cmd: None)
    service = SkillAcquisitionService(
        agent_store=None,  # type: ignore[arg-type]
        agent_cache=None,  # type: ignore[arg-type]
        artifact_store=None,
        stage_root=tmp_path,
    )

    outcome = service.search("image", sources=["skills"])

    assert outcome.results == ()
    assert "skills: skill source unavailable" in outcome.errors[0]


def test_github_source_uses_gh_archive_and_secret_backend_fallback_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_source = tmp_path / "archive-source"
    _write_skill(archive_source / "skills", name="repo-skill")

    class ArchiveRunner(SkillCommandRunner):
        spec: SkillCommandSpec | None = None

        def run(self, spec: SkillCommandSpec, cwd: Path) -> CommandEvidence:
            self.spec = spec
            with tarfile.open(cwd / "github-source.tgz", "w:gz") as archive:
                archive.add(archive_source, arcname="owner-repo-sha")
            return CommandEvidence(
                command=spec.shell or list(spec.argv or ()),
                shell=spec.shell is not None,
                exit_code=0,
                duration_ms=1,
                stdout="",
                stderr="",
            )

    def load_secret(name: str) -> str | None:
        return "infisical-token" if name == "GITHUB_TOKEN_FALLBACK" else None

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", load_secret)
    runner = ArchiveRunner()
    service = SkillAcquisitionService(
        agent_store=None,  # type: ignore[arg-type]
        agent_cache=None,  # type: ignore[arg-type]
        artifact_store=None,
        runner=runner,
        stage_root=tmp_path,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    evidence = service._resolve_source(
        source="github",
        source_ref="https://github.com/ByteDeskAI/example/tree/main/skills",
        command=None,
        workspace=workspace,
        selected_skill_names=[],
    )

    assert evidence.exit_code == 0
    assert runner.spec is not None
    assert runner.spec.shell == "gh api repos/ByteDeskAI/example/tarball/main > github-source.tgz"
    assert runner.spec.env["GH_TOKEN"] == "infisical-token"
    assert runner.spec.env["GITHUB_TOKEN"] == "infisical-token"
    assert [package.name for package in discover_skill_packages(workspace)] == ["repo-skill"]


class _FakeResponse:
    """Minimal context-manager stand-in for an ``http.client`` response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self, amt: int | None = None) -> bytes:
        return self._body if amt is None else self._body[:amt]


def _fake_urlopen(routes: dict[str, bytes]):
    def _open(request, timeout=None):
        url = request.full_url
        if url not in routes:
            raise AssertionError(f"unexpected supercharge URL: {url}")
        return _FakeResponse(routes[url])

    return _open


def _supercharge_service(tmp_path: Path) -> SkillAcquisitionService:
    return SkillAcquisitionService(
        agent_store=None,  # type: ignore[arg-type]
        agent_cache=None,  # type: ignore[arg-type]
        artifact_store=None,
        stage_root=tmp_path,
    )


_SUPERCHARGE_PLUGINS = {
    "success": True,
    "data": [
        {
            "slug": "dogfood",
            "name": "dogfood",
            "description": "Verify any change at the outermost layer.",
            "tags": ["test", "verify"],
            "version": "1.0.0",
            "repositoryUrl": "https://github.com/example/dogfood",
            "files": [{"fileName": "skills/dogfood/SKILL.md", "s3Url": "https://s3/x"}],
        },
        {
            # No SKILL.md → not installable into an omnigent agent → filtered out.
            "slug": "tasks",
            "name": "tasks",
            "description": "Hooks-only plugin.",
            "tags": [],
            "files": [{"fileName": "hooks.json"}],
        },
    ],
}


def test_supercharge_source_listed_and_always_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # supercharge has no external-command requirement, so it stays available
    # even when no CLI tools are installed (unlike skills/npm/github).
    monkeypatch.setattr("omnigent.skills.acquisition.shutil.which", lambda _cmd: None)
    sources = {source["id"]: source for source in _supercharge_service(tmp_path).sources()}

    assert "supercharge" in sources
    assert sources["supercharge"]["available"] is True
    assert sources["supercharge"]["supports_search"] is True


def test_search_supercharge_filters_to_skill_shaped_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = {f"{acq._SUPERCHARGE_BASE}/api/plugins": json.dumps(_SUPERCHARGE_PLUGINS).encode()}
    monkeypatch.setattr(acq, "urlopen", _fake_urlopen(routes))

    hits = _supercharge_service(tmp_path)._search_supercharge("verify", limit=20)

    assert [hit.name for hit in hits] == ["dogfood"]  # 'tasks' has no SKILL.md
    assert hits[0].source == "supercharge"
    assert hits[0].source_ref == "dogfood"
    assert hits[0].version == "1.0.0"


def test_search_supercharge_empty_query_excludes_non_skill_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = {f"{acq._SUPERCHARGE_BASE}/api/plugins": json.dumps(_SUPERCHARGE_PLUGINS).encode()}
    monkeypatch.setattr(acq, "urlopen", _fake_urlopen(routes))

    hits = _supercharge_service(tmp_path)._search_supercharge("", limit=20)

    assert [hit.name for hit in hits] == ["dogfood"]


def test_resolve_supercharge_downloads_files_and_discovers_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_md = b"---\nname: dogfood\ndescription: Verify at the outermost layer.\n---\nUse it.\n"
    base = acq._SUPERCHARGE_BASE
    routes = {
        f"{base}/api/plugins/dogfood": json.dumps(
            {"success": True, "data": {"name": "dogfood", "files": ["skills/dogfood/SKILL.md"]}}
        ).encode(),
        f"{base}/api/plugins/dogfood/skills/dogfood/SKILL.md": skill_md,
    }
    monkeypatch.setattr(acq, "urlopen", _fake_urlopen(routes))
    service = _supercharge_service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert service._resolve_supercharge("dogfood", workspace) is None
    assert (workspace / "skills" / "dogfood" / "SKILL.md").read_bytes() == skill_md
    assert [package.name for package in discover_skill_packages(workspace)] == ["dogfood"]


def test_resolve_supercharge_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = acq._SUPERCHARGE_BASE
    routes = {
        f"{base}/api/plugins/evil": json.dumps(
            {"success": True, "data": {"files": ["../escape.txt"]}}
        ).encode(),
    }
    monkeypatch.setattr(acq, "urlopen", _fake_urlopen(routes))
    service = _supercharge_service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(OmnigentError, match="invalid path segment"):
        service._resolve_supercharge("evil", workspace)


_FIXTURE_CATALOG = Path(__file__).resolve().parents[1] / "resources" / "skills" / "bytedesk-marketplace-catalog.json"


def test_parse_github_marketplace_source_ref_plugin_shape() -> None:
    owner, repo, plugin_path, skill_name, git_ref = acq._parse_github_marketplace_source_ref(
        "ByteDeskAI/bytedesk-marketplace@platform-dev"
    )

    assert owner == "ByteDeskAI"
    assert repo == "bytedesk-marketplace"
    assert plugin_path == "platform-dev"
    assert skill_name is None
    assert git_ref == "main"


def test_parse_github_marketplace_source_ref_skill_path_and_hash_ref() -> None:
    owner, repo, plugin_path, skill_name, git_ref = acq._parse_github_marketplace_source_ref(
        "ByteDeskAI/bytedesk-marketplace/platform-dev/bytedesk-architect#develop"
    )

    assert plugin_path == "platform-dev"
    assert skill_name == "bytedesk-architect"
    assert git_ref == "develop"


def test_search_github_marketplace_uses_pinned_catalog_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_bytes = _FIXTURE_CATALOG.read_bytes()
    url = acq._github_marketplace_catalog_url("ByteDeskAI", "bytedesk-marketplace", "main")
    monkeypatch.setattr(acq, "urlopen", _fake_urlopen({url: catalog_bytes}))

    hits = _supercharge_service(tmp_path)._search_github_marketplace(
        "platform architect",
        repos=("ByteDeskAI/bytedesk-marketplace",),
        limit=20,
    )

    names = [hit.name for hit in hits]
    assert "platform-dev" in names
    assert "platform-architecture" in names
    assert all(hit.source == "github_marketplace" for hit in hits)
    by_name = {hit.name: hit for hit in hits}
    assert by_name["platform-dev"].source_ref == "ByteDeskAI/bytedesk-marketplace@platform-dev"


def test_resolve_github_marketplace_materializes_plugin_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_source = tmp_path / "archive-source"
    _write_skill(archive_source / "platform-dev" / "skills", name="bytedesk-architect")

    class ArchiveRunner(SkillCommandRunner):
        spec: SkillCommandSpec | None = None

        def run(self, spec: SkillCommandSpec, cwd: Path) -> CommandEvidence:
            self.spec = spec
            with tarfile.open(cwd / acq._GITHUB_ARCHIVE_NAME, "w:gz") as archive:
                archive.add(archive_source, arcname="ByteDeskAI-bytedesk-marketplace-deadbeef")
            return CommandEvidence(
                command=spec.shell or list(spec.argv or ()),
                shell=spec.shell is not None,
                exit_code=0,
                duration_ms=1,
                stdout="",
                stderr="",
            )

    monkeypatch.setattr("omnigent.skills.acquisition.shutil.which", lambda cmd: "gh" if cmd == "gh" else None)
    runner = ArchiveRunner()
    service = SkillAcquisitionService(
        agent_store=None,  # type: ignore[arg-type]
        agent_cache=None,  # type: ignore[arg-type]
        artifact_store=None,
        runner=runner,
        stage_root=tmp_path,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    evidence = service._resolve_source(
        source="github_marketplace",
        source_ref="ByteDeskAI/bytedesk-marketplace@platform-dev",
        command=None,
        workspace=workspace,
        selected_skill_names=[],
    )

    assert evidence is not None and evidence.exit_code == 0
    assert [package.name for package in discover_skill_packages(workspace)] == ["bytedesk-architect"]


def test_github_marketplace_source_registered_with_search(
    tmp_path: Path,
) -> None:
    sources = {row["id"]: row for row in _supercharge_service(tmp_path).sources()}

    assert "github_marketplace" in sources
    assert sources["github_marketplace"]["supports_search"] is True
    assert sources["github_marketplace"]["supports_preview"] is True
