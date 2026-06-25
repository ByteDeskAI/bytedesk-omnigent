"""Tests for role-aware skill recommendations."""

from __future__ import annotations

from omnigent.skills.recommendations import recommend_skills_for_role


def test_recommend_skills_for_engineering_developer() -> None:
    rows = recommend_skills_for_role(department="Engineering", title="Platform Developer")

    names = [row.name for row in rows]
    assert "platform-dev" in names
    assert all(row.source == "github_marketplace" for row in rows)


def test_recommend_skills_for_architect_title() -> None:
    rows = recommend_skills_for_role(title="Solutions Architect")

    assert "platform-architecture" in [row.name for row in rows]