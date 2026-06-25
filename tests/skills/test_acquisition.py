"""Unit tests for the skill acquisition framework."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
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
