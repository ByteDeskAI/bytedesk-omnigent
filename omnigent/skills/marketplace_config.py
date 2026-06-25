"""Skills marketplace defaults loaded from server YAML and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from omnigent.server.server_config import config_str_list, load_server_config

_DEFAULT_SEARCH_SOURCES: tuple[str, ...] = ("skills", "npm", "github_marketplace")
_DEFAULT_GITHUB_MARKETPLACE_REPOS: tuple[str, ...] = ("ByteDeskAI/bytedesk-marketplace",)
_DEFAULT_GITHUB_MARKETPLACE_REF = "main"


@dataclass(frozen=True)
class SkillsMarketplaceConfig:
    """Runtime defaults for skill acquisition search and GitHub marketplaces."""

    default_search_sources: tuple[str, ...] = _DEFAULT_SEARCH_SOURCES
    github_marketplace_repos: tuple[str, ...] = _DEFAULT_GITHUB_MARKETPLACE_REPOS
    github_marketplace_ref: str = _DEFAULT_GITHUB_MARKETPLACE_REF


def _skills_section(server_config: dict[str, Any]) -> dict[str, Any]:
    skills = server_config.get("skills")
    return skills if isinstance(skills, dict) else {}


def _github_marketplace_section(skills_section: dict[str, Any]) -> dict[str, Any]:
    github = skills_section.get("github_marketplace")
    return github if isinstance(github, dict) else {}


def load_skills_marketplace_config(
    server_config: dict[str, Any] | None = None,
) -> SkillsMarketplaceConfig:
    """Resolve skills marketplace defaults (YAML first, env overrides)."""
    cfg = server_config if server_config is not None else load_server_config()
    skills_section = _skills_section(cfg)
    github_section = _github_marketplace_section(skills_section)

    search_sources = config_str_list(skills_section.get("default_search_sources"))
    if not search_sources:
        env_sources = os.environ.get("OMNIGENT_SKILLS_DEFAULT_SEARCH_SOURCES", "").strip()
        if env_sources:
            search_sources = [part.strip() for part in env_sources.split(",") if part.strip()]
    if not search_sources:
        search_sources = list(_DEFAULT_SEARCH_SOURCES)

    repos = config_str_list(github_section.get("default_repos"))
    if not repos:
        env_repos = os.environ.get("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REPOS", "").strip()
        if env_repos:
            repos = [part.strip() for part in env_repos.split(",") if part.strip()]
    if not repos:
        repos = list(_DEFAULT_GITHUB_MARKETPLACE_REPOS)

    git_ref = str(github_section.get("default_ref") or "").strip()
    if not git_ref:
        git_ref = os.environ.get("OMNIGENT_SKILLS_GITHUB_MARKETPLACE_REF", "").strip()
    if not git_ref:
        git_ref = _DEFAULT_GITHUB_MARKETPLACE_REF

    return SkillsMarketplaceConfig(
        default_search_sources=tuple(search_sources),
        github_marketplace_repos=tuple(repos),
        github_marketplace_ref=git_ref,
    )