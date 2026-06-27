"""Economic rebalancer — redeploy idle budget from stalled scopes to high-ROI
scopes (BDP-2596 Wave 3, feature 2).

The accountability tick already reopens stalled goals + escalates blocked ones.
This adds the economic half: a stalled goal's scope is, by definition, holding
budget it is not converting (no realized value, owner dropped). Among the goals
reopened *this tick* we rank scopes by ROI and harvest the idle headroom
(``cap - spent``) from the lower-ROI scopes into the single highest-ROI scope,
recorded as treasury decisions for replay.

Driven off the reopened set (not a standing scan), so it is naturally idempotent
under the accountability advisory lock: a second tick with nothing newly stalled
harvests nothing. Realized value is never touched here (booked only by
``book_outcome``); this moves *caps*, the funding ceiling — the flywheel still owns
spend.

ADR-0009: cap moves are guarded by ``set_budget`` (the existing single-writer admin
write); the whole pass runs inside the lock-held accountability tick.
"""
from __future__ import annotations

import uuid
from typing import Any

from bytedesk_omnigent.goals import roi


def rebalance_budget(
    treasury,
    reopened: list[Any],
    *,
    now: int | None = None,
) -> int:
    """Redeploy idle headroom across the reopened goals' scopes, low-ROI → high-ROI.

    :param reopened: the goals reopened this tick (stalled). The highest-ROI scope
        among them is the redeploy target; every other reopened scope's idle
        headroom is harvested into it.
    :returns: total cents reallocated.

    Fewer than two distinct scopes, or no headroom to move → 0. The treasury must be
    the SqlAlchemyTreasury (needs ``remaining_cents`` / ``set_budget`` /
    ``record_decision`` / ``engine``).
    """
    if not reopened or treasury is None:
        return 0
    if not all(
        hasattr(treasury, attr)
        for attr in ("remaining_cents", "set_budget", "record_decision", "engine")
    ):
        return 0

    target = _best_roi_scope(reopened, treasury)
    if target is None:
        return 0

    tick_id = uuid.uuid4().hex
    reallocated = 0
    harvested_scopes: set[tuple[str, str]] = set()
    for goal in reopened:
        scope = (goal.tier, goal.target_id)
        if scope in harvested_scopes or scope == target:
            continue  # don't harvest a scope twice, or the target itself
        headroom = treasury.remaining_cents(*scope)
        if not headroom or headroom <= 0:
            continue
        harvested_scopes.add(scope)
        # Lower the stalled scope's cap by its idle headroom; raise the target's.
        _shift_cap(treasury, scope, -headroom, now=now)
        _shift_cap(treasury, target, headroom, now=now)
        reallocated += headroom
        treasury.record_decision(
            tick_id=tick_id,
            goal_id=goal.id,
            roi_at_decision=roi(goal, remaining_budget_cents=max(headroom, 1)),
            reason="rebalance_redeploy",
            budget_before=headroom,
            budget_after=0,
            now=now,
        )
    return reallocated


def _best_roi_scope(reopened: list[Any], treasury) -> tuple[str, str] | None:
    """The (tier, target_id) of the highest-ROI reopened goal's scope, or ``None``.

    Ranks the reopened goals by their derived ROI against their scope's remaining
    headroom; ties break on the goal id for determinism.
    """
    best: tuple[float, str, tuple[str, str]] | None = None
    for goal in reopened:
        remaining = treasury.remaining_cents(goal.tier, goal.target_id)
        denom = remaining if isinstance(remaining, int) and remaining > 0 else 1
        score = roi(goal, remaining_budget_cents=denom)
        scope = (goal.tier, goal.target_id)
        key = (score, goal.id, scope)
        if best is None or (key[0], key[1]) > (best[0], best[1]):
            best = key
    return best[2] if best is not None else None


def _shift_cap(treasury, scope: tuple[str, str], delta: int, *, now: int | None) -> None:
    """Adjust a scope's cap by ``delta`` (preserving spent), via the admin write.

    Reads the live row to keep its other knobs, then writes the new cap with
    ``set_budget``. ``set_budget`` resets spent? No — it upserts cap/limits only and
    leaves ``spent_cents`` untouched, so remaining moves by exactly ``delta``.
    """
    tier, target_id = scope
    row = _budget_row(treasury, tier, target_id)
    current_cap = row.cap_cents if row is not None else 0
    new_cap = max(0, current_cap + delta)
    treasury.set_budget(
        tier=tier,
        target_id=target_id,
        cap_cents=new_cap,
        cap_tokens=row.cap_tokens if row is not None else None,
        max_spawns=row.max_spawns if row is not None else None,
        anomaly_threshold_cents=row.anomaly_threshold_cents if row is not None else None,
        now=now,
    )


def _budget_row(treasury, tier: str, target_id: str):
    from sqlalchemy.orm import Session

    from bytedesk_omnigent.db_models import SqlGoalBudget

    with Session(treasury.engine) as session:
        return session.get(SqlGoalBudget, (tier, target_id))


__all__ = ["rebalance_budget"]
