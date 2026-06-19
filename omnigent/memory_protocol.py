"""Blessed shared-memory compartments — the org-memory protocol (BDP-2276 D6/E1, ADR-0142).

Two standing conventions over the existing ``team`` / ``topic`` memory scopes so
every agent converges on the same shared spaces instead of inventing ad-hoc
compartment names:

- ``team/org-context`` — the standing org blackboard. Agents append durable
  org-wide context (decisions, standing facts, "what we're doing this week") and
  recall it to coordinate. Always surfaced in ``memory_compartments_list`` so it
  is discoverable even before its first write.
- ``topic/initiative:{id}`` — a per-initiative log. Agents append
  status / blockers / decisions for an initiative and recall the thread before a
  decision turn.

These are naming conventions, not new scopes — the memory store still enforces
server-stamped ownership (ADR-0133/0136); ``team`` / ``topic`` owners are fixed
server-side, so the convention only governs the compartment *name*.
"""

from __future__ import annotations

import re
from typing import Any

# The standing org blackboard lives in the shared ``team`` scope.
ORG_CONTEXT_SCOPE = "team"
ORG_CONTEXT_COMPARTMENT = "org-context"

# Per-initiative logs live in the shared ``topic`` scope under this prefix.
INITIATIVE_SCOPE = "topic"
_INITIATIVE_PREFIX = "initiative:"
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def initiative_compartment(initiative_id: str) -> str:
    """Return the blessed ``topic`` compartment name for an initiative.

    :param initiative_id: An initiative identifier, e.g. ``"BDP-2276"`` or
        ``"Q3 Launch"``.
    :returns: The compartment name, e.g. ``"initiative:bdp-2276"``.
    :raises ValueError: When *initiative_id* has no alphanumeric content.
    """
    slug = _NON_SLUG.sub("-", initiative_id.strip().lower()).strip("-")
    if not slug:
        raise ValueError("initiative_id must contain at least one alphanumeric char")
    return f"{_INITIATIVE_PREFIX}{slug}"


def ensure_org_compartments(
    compartments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Guarantee the standing ``team/org-context`` blackboard is listed.

    The compartment store only lists compartments that already hold a row, so a
    never-written org blackboard would be invisible to agents. Union it in
    (idempotently, at the front) so the shared blackboard is always discoverable.

    :param compartments: Raw ``{"scope", "name", ...}`` dicts from the store.
    :returns: A new list with ``team/org-context`` guaranteed present exactly
        once.
    """
    has_org = any(
        c.get("scope") == ORG_CONTEXT_SCOPE and c.get("name") == ORG_CONTEXT_COMPARTMENT
        for c in compartments
    )
    if has_org:
        return list(compartments)
    return [
        {"scope": ORG_CONTEXT_SCOPE, "name": ORG_CONTEXT_COMPARTMENT},
        *compartments,
    ]
