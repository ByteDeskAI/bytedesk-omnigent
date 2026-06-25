"""Tests for skills marketplace configuration defaults."""

from __future__ import annotations

import pytest

from omnigent.skills.marketplace_config import (
    SkillsMarketplaceConfig,
    load_skills_marketplace_config,
)


def test_load_skills_marketplace_config_uses_yaml_section() -> None:
    cfg = load_skills_marketplace_config(
        {
            "skills": {
                "default_search_sources": ["github_marketplace", "skills"],
                "github_marketplace": {
                    "default_repos": ["Acme/widgets"],
                    "default_ref": "develop",
                },
            }
        }
    )

    assert cfg.default_search_sources == ("github_marketplace", "skills")
    assert cfg.github_marketplace_repos == ("Acme/widgets",)
    assert cfg.github_marketplace_ref == "develop"


def test_load_skills_marketplace_config_falls_back_to_byteDesk_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_SKILLS_DEFAULT_SEARCH_SOURCES", raising=False)
    monkeypatch.delenv("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REPOS", raising=False)
    monkeypatch.delenv("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REF", raising=False)

    cfg = load_skills_marketplace_config({})

    assert cfg == SkillsMarketplaceConfig()
    assert "github_marketplace" in cfg.default_search_sources
    assert cfg.github_marketplace_repos == ("ByteDeskAI/bytedesk-marketplace",)


def test_load_skills_marketplace_config_env_overrides_empty_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_SKILLS_DEFAULT_SEARCH_SOURCES", "npm,github_marketplace")
    monkeypatch.setenv("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REPOS", "ByteDeskAI/other-repo")
    monkeypatch.setenv("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REF", "release")

    cfg = load_skills_marketplace_config({})

    assert cfg.default_search_sources == ("npm", "github_marketplace")
    assert cfg.github_marketplace_repos == ("ByteDeskAI/other-repo",)
    assert cfg.github_marketplace_ref == "release"