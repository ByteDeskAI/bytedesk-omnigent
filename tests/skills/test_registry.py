"""Tests for the skill marketplace registry."""

from __future__ import annotations

from omnigent.skills.marketplace_config import SkillsMarketplaceConfig
from omnigent.skills.registry import skill_marketplace_entries


def test_skill_marketplace_entries_includes_bytedesk_github_catalog() -> None:
    entries = skill_marketplace_entries(
        SkillsMarketplaceConfig(
            github_marketplace_repos=("ByteDeskAI/bytedesk-marketplace",),
        )
    )
    bytedesk = next(entry for entry in entries if entry.repo == "ByteDeskAI/bytedesk-marketplace")

    assert bytedesk.id == "github:ByteDeskAI-bytedesk-marketplace"
    assert bytedesk.label == "ByteDesk Catalog"
    assert bytedesk.source_id == "github_marketplace"
    assert bytedesk.default is True


def test_skill_marketplace_entries_lists_supercharge_and_skills_sh() -> None:
    entries = skill_marketplace_entries(SkillsMarketplaceConfig())
    ids = {entry.id for entry in entries}

    assert "supercharge" in ids
    assert "skills-sh" in ids