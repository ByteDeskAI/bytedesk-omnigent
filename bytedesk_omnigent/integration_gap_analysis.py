"""Deterministic gap analysis for the integration capability catalog.

This module lets autonomous planning loops and ByteDesk Platform surfaces compare
known implemented/open integration work against the static capability catalog. It
is intentionally pure: callers supply implementation/open-work evidence and get a
JSON-ready report without reading git, GitHub, credentials, or tenant data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    IntegrationCapability,
    get_integration_capability,
    list_integration_capabilities,
)


@dataclass(frozen=True)
class IntegrationImplementationSignal:
    """Evidence that a catalog capability is already implemented or in flight."""

    slug: str | None
    source: str
    title: str
    url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCapabilityGapReport:
    """Catalog coverage report for autonomous integration planning."""

    total_catalog_entries: int
    implemented_count: int
    open_work_count: int
    covered_slugs: tuple[str, ...]
    next_recommended_slug: str | None
    gaps: tuple[IntegrationCapability, ...]
    open_work: tuple[IntegrationImplementationSignal, ...]

    def to_dict(self) -> dict:
        return {
            "object": "integration_capability_gap_report",
            "total_catalog_entries": self.total_catalog_entries,
            "implemented_count": self.implemented_count,
            "open_work_count": self.open_work_count,
            "covered_slugs": list(self.covered_slugs),
            "next_recommended_slug": self.next_recommended_slug,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "open_work": [signal.to_dict() for signal in self.open_work],
        }


def analyze_integration_capability_gaps(
    *,
    implemented_slugs: set[str] | None = None,
    open_signals: tuple[IntegrationImplementationSignal, ...] = (),
) -> IntegrationCapabilityGapReport:
    """Return uncovered catalog entries after implemented and open work.

    Entries stay in catalog priority order so planning agents can safely choose
    ``next_recommended_slug`` as the highest-value capability not already covered
    by local implementation evidence or an open loop PR.
    """

    catalog = tuple(list_integration_capabilities())
    catalog_slugs = {entry.slug for entry in catalog}
    implemented = tuple(sorted((implemented_slugs or set()) & catalog_slugs))
    resolved_open_work: list[IntegrationImplementationSignal] = []
    for signal in open_signals:
        resolved_signal = _resolve_signal(signal)
        if resolved_signal is not None:
            resolved_open_work.append(resolved_signal)
    open_slugs = tuple(sorted({signal.slug for signal in resolved_open_work if signal.slug}))
    covered = tuple(sorted(set(implemented) | set(open_slugs)))
    gaps = tuple(entry for entry in catalog if entry.slug not in covered)

    return IntegrationCapabilityGapReport(
        total_catalog_entries=len(catalog),
        implemented_count=len(implemented),
        open_work_count=len(resolved_open_work),
        covered_slugs=covered,
        next_recommended_slug=gaps[0].slug if gaps else None,
        gaps=gaps,
        open_work=tuple(resolved_open_work),
    )


def _resolve_signal(
    signal: IntegrationImplementationSignal,
) -> IntegrationImplementationSignal | None:
    if signal.slug and get_integration_capability(signal.slug) is not None:
        return signal

    matched_slug = _match_catalog_slug_from_title(signal.title)
    if matched_slug is None:
        return None

    return IntegrationImplementationSignal(
        slug=matched_slug,
        source=signal.source,
        title=signal.title,
        url=signal.url,
    )


def _match_catalog_slug_from_title(title: str) -> str | None:
    words = set(_tokenize(title))
    best_slug: str | None = None
    best_score = 0

    for capability in list_integration_capabilities():
        identity_tokens = set(_tokenize(capability.slug)) | set(_tokenize(capability.name))
        category_tokens = set(_tokenize(capability.category))
        description_tokens = set(_tokenize(capability.implementation_description))
        score = (
            len(words & identity_tokens) * 3
            + len(words & category_tokens) * 2
            + len(words & description_tokens)
        )
        if score > best_score and score >= 2:
            best_slug = capability.slug
            best_score = score

    return best_slug


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in value)
    return tuple(part for part in normalized.split() if len(part) > 2)
