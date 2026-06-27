"""Swappable strategy seams for the goal engine (BDP-2589, Phase 7).

Optimizer / Treasury / Assignment selection runs through PluggableRegistry seams
exactly like ``goal_sensor``: the built-ins are the registered default; an
``OMNIGENT_USE_<SEAM>`` env (or a registration) swaps the impl without forking.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.optimizer import RoiOptimizer
from bytedesk_omnigent.engine.registries import (
    build_assignment_registry,
    build_optimizer_registry,
    build_treasury_registry,
)


def test_optimizer_default_is_roi() -> None:
    reg = build_optimizer_registry()
    assert isinstance(reg.resolve_default(), RoiOptimizer)


def test_optimizer_env_override_swaps(monkeypatch) -> None:
    sentinel = object()
    reg = build_optimizer_registry()
    reg.register("custom", lambda: sentinel)
    monkeypatch.setenv("OMNIGENT_USE_GOAL_OPTIMIZER", "custom")
    assert reg.resolve_default() is sentinel


def test_treasury_default_resolves(tmp_path) -> None:
    from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury

    loc = f"sqlite:///{tmp_path / 'g.db'}"
    reg = build_treasury_registry(loc)
    assert isinstance(reg.resolve_default(), SqlAlchemyTreasury)


def test_assignment_default_resolves() -> None:
    reg = build_assignment_registry()
    policy = reg.resolve_default()
    # The default assignment policy exposes the resolver entrypoint.
    assert callable(getattr(policy, "resolve_assignee", None))


def test_assignment_env_override_swaps(monkeypatch) -> None:
    sentinel = object()
    reg = build_assignment_registry()
    reg.register("custom", lambda: sentinel)
    monkeypatch.setenv("OMNIGENT_USE_GOAL_ASSIGNMENT", "custom")
    assert reg.resolve_default() is sentinel


def test_optimizer_respects_config_risk_decay() -> None:
    # A tenant that boosts high-risk decay re-ranks a high-EV high-risk goal above
    # a safe one — proving goals.roi.risk_decay.* is a LIVE knob, not dead config.
    from dataclasses import dataclass

    @dataclass
    class _G:
        id: str
        risk_tier: str
        expected_value_cents: int
        confidence: float = 1.0
        priority: int = 3
        created_at: int = 0

    low = _G(id="low", risk_tier="low", expected_value_cents=100)
    high = _G(id="high", risk_tier="high", expected_value_cents=110)
    # Default decay (high=0.4): low(100*1.0=100) outranks high(110*0.4=44).
    assert [g.id for g in RoiOptimizer().rank([low, high], now=0)] == ["low", "high"]
    # Boosted decay (high=1.0): high(110) now outranks low(100).
    boosted = RoiOptimizer(risk_decay={"low": 1.0, "medium": 0.7, "high": 1.0})
    assert [g.id for g in boosted.rank([low, high], now=0)] == ["high", "low"]


def test_ready_tenant_ids_collects_distinct_targets(tmp_path) -> None:
    from bytedesk_omnigent.engine.loop import _ready_tenant_ids
    from bytedesk_omnigent.goals import SqlAlchemyGoalStore

    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    for tid in ("acme", "acme", "globex"):
        g = store.create_goal(title="t", target_kind="organization", target_id=tid)
        store.claim_goal(goal_id=g.id, owner_agent_id="maya")
    # An unowned goal is not in the ready frontier.
    store.create_goal(title="unowned", target_kind="organization", target_id="zzz")
    assert _ready_tenant_ids(store) == {"acme", "globex"}
