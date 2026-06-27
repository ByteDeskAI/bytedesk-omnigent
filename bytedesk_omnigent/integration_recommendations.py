"""Goal-scored recommendations for integration capability selection.

The static catalog says what Omnigent can build next. This module lets a
planning loop, platform UI, or operator submit a natural-language business goal
and receive deterministic, secret-free recommendations for the catalog entries
most likely to satisfy that goal.
"""

from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    IntegrationCapability,
    list_integration_capabilities,
)


@dataclass(frozen=True)
class IntegrationCapabilityRecommendation:
    """One catalog recommendation with deterministic scoring evidence."""

    slug: str
    match_score: int
    matched_signals: tuple[str, ...]
    rationale: str
    capability: IntegrationCapability

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "match_score": self.match_score,
            "matched_signals": list(self.matched_signals),
            "rationale": self.rationale,
            "capability": self.capability.to_dict(),
        }


@dataclass(frozen=True)
class IntegrationCapabilityRecommendationReport:
    """A JSON-ready report for goal-based integration planning."""

    goal: str
    category: CapabilityCategory | None
    limit: int
    recommendations: tuple[IntegrationCapabilityRecommendation, ...]

    def to_dict(self) -> dict:
        return {
            "object": "integration_capability_recommendation_report",
            "goal": self.goal,
            "category": self.category,
            "limit": self.limit,
            "recommendations": [entry.to_dict() for entry in self.recommendations],
        }


_CATEGORY_HINTS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": ("slack", "channel", "thread", "message", "approval", "escalation", "chat"),
    "project_management": (
        "issue",
        "jira",
        "linear",
        "trello",
        "card",
        "backlog",
        "sprint",
        "status",
    ),
    "knowledge": (
        "notion",
        "docs",
        "document",
        "drive",
        "sheet",
        "calendar",
        "meeting",
        "memory",
        "knowledge",
    ),
    "developer": (
        "github",
        "repo",
        "repository",
        "pr",
        "pull",
        "ci",
        "review",
        "checks",
        "engineering",
        "code",
    ),
    "crm_support": (
        "support",
        "ticket",
        "customer",
        "crm",
        "salesforce",
        "hubspot",
        "zendesk",
        "intercom",
        "triage",
    ),
    "commerce_billing": (
        "stripe",
        "shopify",
        "invoice",
        "subscription",
        "order",
        "refund",
        "billing",
        "payment",
        "revenue",
    ),
    "workflow_harness": (
        "workflow",
        "blueprint",
        "phase",
        "deterministic",
        "harness",
        "template",
        "repeatable",
    ),
}

_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "ci": ("ci", "checks", "failed", "build"),
    "docs": ("docs", "document", "documents", "page", "pages"),
    "pr": ("pr", "pull", "review"),
    "tickets": ("ticket", "tickets", "support"),
}

_STOPWORDS = {"and", "for", "from", "into", "that", "the", "with"}


def recommend_integration_capabilities(
    goal: str,
    *,
    category: CapabilityCategory | None = None,
    limit: int = 3,
) -> IntegrationCapabilityRecommendationReport:
    """Rank integration capabilities for a natural-language business goal.

    The scorer is deliberately deterministic and local: it tokenizes the goal,
    compares it with catalog fields plus category hints, and uses catalog priority
    as a stable tie-breaker. It never calls external services or reads secrets.
    """

    normalized_goal = " ".join(goal.split())
    goal_tokens = set(_expand_tokens(_tokenize(normalized_goal)))
    entries = list_integration_capabilities(category=category)
    scored = [_score_capability(entry, goal_tokens, normalized_goal) for entry in entries]
    scored.sort(key=lambda item: (item.match_score, item.capability.priority_score), reverse=True)
    return IntegrationCapabilityRecommendationReport(
        goal=normalized_goal,
        category=category,
        limit=limit,
        recommendations=tuple(scored[:limit]),
    )


def _score_capability(
    capability: IntegrationCapability,
    goal_tokens: set[str],
    goal: str,
) -> IntegrationCapabilityRecommendation:
    catalog_tokens = set(
        _tokenize(
            " ".join(
                (
                    capability.slug,
                    capability.name,
                    capability.category,
                    capability.auth_model,
                    capability.implementation_description,
                    capability.business_case,
                    " ".join(capability.agent_value),
                    " ".join(capability.future_unlocks),
                    " ".join(capability.required_scopes),
                )
            )
        )
    )
    category_hits = goal_tokens & set(_CATEGORY_HINTS[capability.category])
    catalog_hits = goal_tokens & catalog_tokens
    matched_signals = set(category_hits | catalog_hits) - _STOPWORDS
    if category_hits:
        matched_signals.add(capability.category)
    matched = tuple(sorted(matched_signals))
    score = len(catalog_hits) * 3 + len(category_hits) * 5 + capability.priority_score // 25
    rationale = _rationale(capability, matched, goal)
    return IntegrationCapabilityRecommendation(
        slug=capability.slug,
        match_score=score,
        matched_signals=matched,
        rationale=rationale,
        capability=capability,
    )


def _rationale(capability: IntegrationCapability, matched: tuple[str, ...], goal: str) -> str:
    signal_text = (
        ", ".join(_display_signal(signal) for signal in matched[:5]) or "catalog priority"
    )
    return (
        f"{capability.name} is recommended because the goal mentions {signal_text}, "
        f"which maps to its {capability.category} integration value for: {goal}."
    )


def _display_signal(signal: str) -> str:
    if signal == "ci":
        return "CI"
    if signal == "pr":
        return "PR"
    return signal


def _expand_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(_TOKEN_ALIASES.get(token, ()))
    return tuple(expanded)


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in value)
    return tuple(part for part in normalized.split() if len(part) > 1)
