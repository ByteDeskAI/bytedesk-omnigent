"""Unit tests for the skill acquisition framework."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.skills.acquisition import discover_skill_packages


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

