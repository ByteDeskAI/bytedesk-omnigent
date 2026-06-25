"""Unit tests for the skill acquisition framework."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.skills.acquisition import (
    SkillAcquisitionService,
    SkillCommandRunner,
    SkillCommandSpec,
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
