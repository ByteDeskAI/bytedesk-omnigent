"""Role-aware skill recommendations from department and title metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillRecommendation:
    """One suggested marketplace plugin or skill ref for a role context."""

    name: str
    source: str
    source_ref: str
    reason: str


_DEPARTMENT_PLUGINS: dict[str, tuple[str, ...]] = {
    "engineering": ("platform-dev", "omnigent-dev", "platform-architecture"),
    "operations": ("platform-ops", "bytedesk-goals", "project-management"),
    "growth": ("platform-frontend", "design-patterns", "structurizr"),
    "product": ("project-management", "platform-domain", "bytedesk-goals"),
}

_TITLE_PLUGINS: dict[str, tuple[str, ...]] = {
    "architect": ("platform-architecture", "structurizr", "design-patterns"),
    "developer": ("platform-dev", "omnigent-dev"),
    "design": ("platform-frontend", "design-patterns"),
}


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def recommend_skills_for_role(
    *,
    department: str | None = None,
    title: str | None = None,
    limit: int = 8,
) -> list[SkillRecommendation]:
    """Return deterministic ByteDesk catalog suggestions for a role context."""
    dept_key = _normalize(department)
    title_key = _normalize(title)
    plugin_names: list[str] = []
    seen: set[str] = set()

    def add_plugins(names: tuple[str, ...], *, reason: str) -> None:
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            plugin_names.append(name)
            if len(plugin_names) >= limit:
                return
            _ = reason

    for key, plugins in _DEPARTMENT_PLUGINS.items():
        if key in dept_key:
            add_plugins(plugins, reason=f"department:{department}")
            break

    for key, plugins in _TITLE_PLUGINS.items():
        if key in title_key:
            add_plugins(plugins, reason=f"title:{title}")
            break

    if not plugin_names:
        add_plugins(
            ("platform-dev", "project-management", "bytedesk-goals"),
            reason="default",
        )

    recommendations: list[SkillRecommendation] = []
    for plugin in plugin_names[:limit]:
        recommendations.append(
            SkillRecommendation(
                name=plugin,
                source="github_marketplace",
                source_ref=f"ByteDeskAI/bytedesk-marketplace@{plugin}",
                reason=(
                    f"Suggested for {department or 'this scope'}"
                    + (f" / {title}" if title else "")
                ),
            )
        )
    return recommendations