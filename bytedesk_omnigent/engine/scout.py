"""Opportunity discovery / scout (BDP-2596 Wave 3, feature 6).

A standing recurring "scout" goal whose dispatched agent scans sensors (leads /
churn / market / idle-capability via the sensor registry) and PROPOSES new goals
as DRAFTS. A proposal is created in a non-actionable state so it enters the
governance gate (draft → approve) rather than auto-arming: it is ``deferred``
(activation ``paused``, never claimable) and carries ``attributes.approval_state =
"proposed"``. Approval is a normal ``activate_goal`` + flag flip (the existing
admin/governance surface), so nothing here auto-activates work.

This module provides the seed template + the deterministic proposal path. The
live agent scan is the dispatched scout agent calling this path (via ``goal_create``
with a proposed flag); the engine never reaches the network here.
"""
from __future__ import annotations

from typing import Any

SCOUT_GOAL_SLUG = "bytedesk.scout.opportunity"
# Daily morning scan by default; cadence is overridable at seed time.
_DEFAULT_SCOUT_CADENCE = "0 8 * * *"


def propose_goal(
    goal_store,
    *,
    title: str,
    source: str,
    rationale: str | None = None,
    expected_value_cents: int = 0,
    risk_tier: str = "low",
    target_kind: str = "organization",
    target_id: str | None = None,
    target_label: str | None = None,
    now: int | None = None,
):
    """Create a goal as a governance DRAFT (deferred + ``approval_state=proposed``).

    A proposed goal is NOT actionable: ``readiness_kind="deferred"`` makes its
    activation ``paused`` so neither the assignment pre-pass nor the dispatcher
    touches it. ``attributes.approval_state="proposed"`` (+ the rationale) is the
    flag the governance surface reads to present it for approval. Returns the
    created :class:`Goal`.
    """
    attributes: dict[str, Any] = {"approval_state": "proposed"}
    if rationale is not None:
        attributes["rationale"] = rationale
    return goal_store.create_goal(
        title=title,
        source=source,
        readiness_kind="deferred",
        payload={"attributes": attributes},
        expected_value_cents=expected_value_cents,
        risk_tier=risk_tier,
        target_kind=target_kind,
        target_id=target_id,
        target_label=target_label,
        now=now,
    )


def scout_seed(*, cadence_expr: str = _DEFAULT_SCOUT_CADENCE) -> dict[str, Any]:
    """The create_goal kwargs for the standing recurring scout goal.

    A recurring goal (it never closes) tagged with :data:`SCOUT_GOAL_SLUG` so it is
    idempotently discoverable. Its dispatched agent scans sensors and files
    proposals via :func:`propose_goal`.
    """
    return {
        "title": "Scan for new opportunities and propose goals",
        "source": "scout",
        "cadence_kind": "recurring",
        "cadence_expr": cadence_expr,
        "target_kind": "organization",
        "payload": {"slug": SCOUT_GOAL_SLUG},
    }


def ensure_scout_goal(goal_store, *, scheduler=None, cadence_expr: str = _DEFAULT_SCOUT_CADENCE,
                      now: int | None = None):
    """Idempotently create the standing scout goal; return the existing/new goal.

    Discovers an existing scout by its ``payload.slug`` so a re-run (boot re-seed)
    never creates a duplicate. The scout goal is claimed by an agent through the
    normal dispatch path; this only ensures the standing recurring goal exists.
    """
    for goal in goal_store.list_goals():
        if (goal.payload or {}).get("slug") == SCOUT_GOAL_SLUG:
            return goal
    seed = scout_seed(cadence_expr=cadence_expr)
    return goal_store.create_goal(scheduler=scheduler, now=now, **seed)


__all__ = [
    "SCOUT_GOAL_SLUG",
    "ensure_scout_goal",
    "propose_goal",
    "scout_seed",
]
