"""Swappable strategy seams for the goal engine (BDP-2589, Phase 7, ADR-0008).

Optimizer / Treasury / AssignmentPolicy selection runs through
:class:`~omnigent.kernel.pluggable.PluggableRegistry` — exactly like the
``goal_sensor`` seam (BDP-2584). Each builder registers the existing built-in as
the default, so a tenant/operator swaps the policy via ``OMNIGENT_USE_<SEAM>``
(or a registration) **without forking** the tick. The tick resolves the active
impl through these registries.
"""

from __future__ import annotations

from omnigent.kernel.pluggable.registry import PluggableRegistry

OPTIMIZER_SEAM = "goal_optimizer"
TREASURY_SEAM = "goal_treasury"
ASSIGNMENT_SEAM = "goal_assignment"


class DefaultAssignmentPolicy:
    """The built-in assignment policy — the capability∩department + scoreboard
    resolver (``assignment.resolve_assignee``) wrapped as a swappable object."""

    def resolve_assignee(self, **kwargs):
        from bytedesk_omnigent.assignment import resolve_assignee

        return resolve_assignee(**kwargs)


def build_optimizer_registry() -> PluggableRegistry:
    """Registry with :class:`RoiOptimizer` as the registered default."""
    from bytedesk_omnigent.engine.optimizer import RoiOptimizer

    return PluggableRegistry(OPTIMIZER_SEAM, default=("roi", RoiOptimizer))


def build_treasury_registry(storage_location: str) -> PluggableRegistry:
    """Registry with :class:`SqlAlchemyTreasury` (bound to ``storage_location``)
    as the registered default."""
    from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury

    return PluggableRegistry(
        TREASURY_SEAM,
        default=("sqlalchemy", lambda: SqlAlchemyTreasury(storage_location)),
    )


def build_assignment_registry() -> PluggableRegistry:
    """Registry with :class:`DefaultAssignmentPolicy` as the registered default.

    BDP-2597: the market-based :class:`BiddingAssignmentPolicy` is also registered
    (under ``"bidding"``) but is NOT the default — select it per-env/tenant via
    ``OMNIGENT_USE_GOAL_ASSIGNMENT=bidding``.
    """
    registry = PluggableRegistry(ASSIGNMENT_SEAM, default=("default", DefaultAssignmentPolicy))

    def _bidding():
        from bytedesk_omnigent.engine.bidding import BiddingAssignmentPolicy

        return BiddingAssignmentPolicy()

    registry.register("bidding", _bidding)
    return registry


__all__ = [
    "ASSIGNMENT_SEAM",
    "OPTIMIZER_SEAM",
    "TREASURY_SEAM",
    "DefaultAssignmentPolicy",
    "build_assignment_registry",
    "build_optimizer_registry",
    "build_treasury_registry",
]
