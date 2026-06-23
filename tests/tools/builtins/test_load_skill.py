"""Tests for omnigent.tools.builtins.load_skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import LoadSkillTool
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
    list_skill_resources,
)


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing resource listing in load_skill output.

    :returns: A ``SkillSpec`` pointing at a real directory
        with a reference file.
    """
    skill_dir = tmp_path / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "style-guide.md").write_text("# Style Guide\n\nUse snake_case.")
    return SkillSpec(
        name="code-review",
        description="Reviews code.",
        content="Review the code.",
        skill_dir=skill_dir,
    )


@pytest.fixture()
def skill_no_resources() -> SkillSpec:
    """
    A skill with no ``skill_dir`` (in-memory only).

    :returns: A ``SkillSpec`` with ``skill_dir=None``.
    """
    return SkillSpec(
        name="summarize",
        description="Summarizes text.",
        content="Summarize the input concisely.",
    )


def test_load_skill_name_and_description() -> None:
    """Identity helpers are wired for tool registration."""
    assert LoadSkillTool.name() == "load_skill"
    assert "skill" in LoadSkillTool.description().lower()


def test_load_skill_skills_property_merges_bundled(
    skill_no_resources: SkillSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skills property exposes bundled skills when host discovery is empty."""
    monkeypatch.setattr(
        "omnigent.spec.parser.discover_host_skills",
        lambda *_args, **_kwargs: [],
    )
    tool = LoadSkillTool([skill_no_resources])
    assert tool.skills == [skill_no_resources]


def test_find_skill_by_name_returns_match_and_none(
    skill_no_resources: SkillSpec,
    skill_with_resources: SkillSpec,
) -> None:
    """Exact-name lookup returns the skill or None when absent."""
    skills = [skill_no_resources, skill_with_resources]
    assert find_skill_by_name(skills, "summarize") is skill_no_resources
    assert find_skill_by_name(skills, "missing") is None


def test_format_skill_meta_text_wraps_skill_and_user_request(
    skill_with_resources: SkillSpec,
) -> None:
    """Slash-command injection includes path, content, and user args."""
    text = format_skill_meta_text(skill_with_resources, "review this diff")
    assert "<skill>" in text
    assert "<name>code-review</name>" in text
    assert "<path>" in text
    assert "SKILL.md" in text
    assert "references/style-guide.md" in text
    assert "<user_request>" in text
    assert "review this diff" in text


def test_format_skill_meta_text_omits_path_without_skill_dir(
    skill_no_resources: SkillSpec,
) -> None:
    """In-memory skills skip the <path> block but still wrap content."""
    text = format_skill_meta_text(skill_no_resources, "")
    assert "<path>" not in text
    assert "Summarize the input concisely." in text
    assert "<user_request>" not in text


def test_list_skill_resources_scans_scripts_and_assets(
    tmp_path: Path,
) -> None:
    """Resource listing covers references, scripts, and assets trees."""
    skill_dir = tmp_path / "demo-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh")
    (skill_dir / "assets" / "logo.png").write_bytes(b"\x89PNG")
    skill = SkillSpec(
        name="demo",
        description="Demo",
        content="Demo content.",
        skill_dir=skill_dir,
    )
    resources = list_skill_resources(skill)
    assert "scripts/run.sh" in resources
    assert "assets/logo.png" in resources


def test_load_skill_returns_content(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns the skill's content string.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "summarize"}), tool_ctx)
    assert result == "Summarize the input concisely."


def test_load_skill_not_found(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error for unknown skill name.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "nonexistent"}), tool_ctx)
    assert "not found" in result
    assert "summarize" in result


def test_load_skill_with_resources_lists_files(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke appends a resource listing when the
    skill has bundled reference files.
    """
    tool = LoadSkillTool([skill_with_resources])
    result = tool.invoke(
        json.dumps({"name": "code-review"}),
        tool_ctx,
    )
    assert "Review the code." in result
    assert "references/style-guide.md" in result
    assert "read_skill_file" in result


def test_load_skill_missing_name_argument(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error when 'name' is missing.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({}), tool_ctx)
    assert "missing required 'name'" in result


def test_load_skill_schema_lists_skill_names(
    skill_no_resources: SkillSpec,
    skill_with_resources: SkillSpec,
) -> None:
    """
    LoadSkillTool.get_schema includes all skill names in the
    description.
    """
    tool = LoadSkillTool(
        [skill_no_resources, skill_with_resources],
    )
    schema = tool.get_schema()
    desc = schema["function"]["description"]
    assert "summarize" in desc
    assert "code-review" in desc
