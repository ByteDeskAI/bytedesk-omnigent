"""Registry of searchable skill marketplaces exposed to UI and concierge."""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.skills.marketplace_config import SkillsMarketplaceConfig


@dataclass(frozen=True)
class SkillMarketplaceEntry:
    """One searchable marketplace surface (may map to an acquisition source)."""

    id: str
    label: str
    source_id: str
    kind: str
    description: str | None = None
    default: bool = False
    repo: str | None = None


def skill_marketplace_entries(
    config: SkillsMarketplaceConfig | None = None,
) -> tuple[SkillMarketplaceEntry, ...]:
    """Return the configured marketplace catalog for search UIs."""
    from omnigent.skills.marketplace_config import load_skills_marketplace_config

    resolved = config or load_skills_marketplace_config()
    entries: list[SkillMarketplaceEntry] = []
    for index, repo in enumerate(resolved.github_marketplace_repos):
        entries.append(
            SkillMarketplaceEntry(
                id=f"github:{repo.replace('/', '-')}",
                label="ByteDesk Catalog" if repo.endswith("bytedesk-marketplace") else repo,
                source_id="github_marketplace",
                kind="github_catalog",
                description=(
                    "Claude-format plugins from "
                    f"{repo}@{resolved.github_marketplace_ref}"
                ),
                default=index == 0,
                repo=repo,
            )
        )
    entries.append(
        SkillMarketplaceEntry(
            id="supercharge",
            label="Supercharge Claude Code",
            source_id="supercharge",
            kind="http_catalog",
            description="Community skill plugins from superchargeclaudecode.com",
            default=False,
        )
    )
    entries.append(
        SkillMarketplaceEntry(
            id="skills-sh",
            label="skills.sh",
            source_id="skills",
            kind="cli_index",
            description="Agent Skills CLI registry",
            default=False,
        )
    )
    return tuple(entries)


def marketplace_entry_to_dict(entry: SkillMarketplaceEntry) -> dict[str, object]:
    """Serialize a registry entry for API responses."""
    return {
        "id": entry.id,
        "label": entry.label,
        "source_id": entry.source_id,
        "kind": entry.kind,
        "description": entry.description,
        "default": entry.default,
        "repo": entry.repo,
    }