"""Governance read model (BDP-2278 F5 backbone, ADR-0142).

A read-only rollup over the durable org stores — the goals backlog (C3), open
deliberations (C6), and the outcome leaderboard (B7) — that the Founder
Governance cockpit (and the control-plane Work tab) reads to see org state at a
glance. Pure + injectable (the stores are passed in), so it is unit-provable
standalone; the FastAPI route (``omnigent/server/routes/governance.py``) is thin
glue, mirroring the ingress route's pure-logic + thin-route split.
"""

from __future__ import annotations


def governance_summary(*, goal_store, deliberation_store) -> dict:
    """Return a one-glance org-state rollup.

    :param goal_store: the goals/scoreboard store (C3).
    :param deliberation_store: the deliberation store (C6).
    :returns: ``{"goals": {"total", "by_status"}, "open_deliberations": [...]}``.
    """
    goals = goal_store.list_goals()
    by_status: dict[str, int] = {}
    for goal in goals:
        by_status[goal.status] = by_status.get(goal.status, 0) + 1
    open_deliberations = [
        {"id": d.id, "topic": d.topic, "opened_by": d.opened_by}
        for d in deliberation_store.list_open()
    ]
    return {
        "goals": {"total": len(goals), "by_status": by_status},
        "open_deliberations": open_deliberations,
    }


def outcome_leaderboard(*, outcome_ledger, metric: str, limit: int = 10) -> dict:
    """Return the ``(agent_id, value)`` leaderboard for a metric as JSON-able rows."""
    rows = outcome_ledger.leaderboard(metric=metric, limit=limit)
    return {
        "metric": metric,
        "leaderboard": [{"agent_id": agent_id, "value": value} for agent_id, value in rows],
    }
