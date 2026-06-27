"""Per-tenant goal-engine config on the runtime-flags plane (BDP-2589, Phase 7).

The engine knobs are runtime flags (NATS-KV, hot-reloadable, ships-dark, per-tenant
via rules) — all SAFE defaults: posture ``gated``, high-risk approval required,
paper-trading default ON, today's interval/decays/caps. A per-tenant override
resolves via a ``tenant`` rule; nothing set resolves to the global default.
"""
from __future__ import annotations

from dataclasses import replace

from bytedesk_omnigent.engine.config import (
    GOAL_AUTONOMY_POSTURE,
    GOAL_REQUIRE_APPROVAL_ALL,
    GOAL_TICK_INTERVAL_SECONDS,
    GoalEngineConfig,
    load_goal_engine_config,
    seed_goal_engine_flags,
)
from bytedesk_omnigent.runtime_flags.models import FlagRule, FlagVariation
from bytedesk_omnigent.runtime_flags.store import InMemoryRuntimeFlagStore


async def test_defaults_are_safe() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store)
    cfg = await load_goal_engine_config("omnigent", store=store)
    assert isinstance(cfg, GoalEngineConfig)
    assert cfg.autonomy_posture == "gated"
    assert cfg.high_risk_required is True
    assert cfg.paper_trading_default is True
    assert cfg.require_approval_all is False
    assert cfg.tick_interval_seconds == 30
    assert cfg.budget_default_cap_cents == 0
    assert cfg.risk_decay == {"low": 1.0, "medium": 0.7, "high": 0.4}
    assert cfg.anomaly_threshold_cents is None


async def test_unseeded_store_yields_safe_defaults() -> None:
    # An unseeded/unreachable flag must fail closed to the safe default.
    cfg = await load_goal_engine_config("omnigent", store=InMemoryRuntimeFlagStore())
    assert cfg.autonomy_posture == "gated"
    assert cfg.high_risk_required is True


async def test_global_posture_flip_to_full_auto() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store)
    rev = await store.get_revision(GOAL_AUTONOMY_POSTURE)
    await store.upsert(
        replace(rev.definition, default_variation="full_auto"), if_match=rev.revision
    )
    cfg = await load_goal_engine_config("omnigent", store=store)
    assert cfg.autonomy_posture == "full_auto"


async def test_per_tenant_override_resolves() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store)
    # Arm full_auto for tenant "acme" only, via a tenant rule.
    rev = await store.get_revision(GOAL_AUTONOMY_POSTURE)
    rule = FlagRule(attribute="tenant", op="equals", values=("acme",), variation="full_auto")
    armed = replace(rev.definition, rules=(rule,))
    await store.upsert(armed, if_match=rev.revision)

    acme = await load_goal_engine_config("acme", store=store)
    other = await load_goal_engine_config("omnigent", store=store)
    assert acme.autonomy_posture == "full_auto"
    assert other.autonomy_posture == "gated"  # global default untouched


async def test_require_approval_all_is_per_tenant() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store)
    rev = await store.get_revision(GOAL_REQUIRE_APPROVAL_ALL)
    await store.upsert(
        replace(
            rev.definition,
            rules=(FlagRule(attribute="tenant", op="equals", values=("acme",), variation="on"),),
        ),
        if_match=rev.revision,
    )
    assert (await load_goal_engine_config("acme", store=store)).require_approval_all is True
    assert (await load_goal_engine_config("omnigent", store=store)).require_approval_all is False


async def test_numeric_interval_per_tenant_override() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store)
    rev = await store.get_revision(GOAL_TICK_INTERVAL_SECONDS)
    # Add a named variation with a faster value and route "acme" to it by rule.
    bumped = replace(
        rev.definition,
        variations=(*rev.definition.variations, FlagVariation("fast", 5)),
        rules=(FlagRule(attribute="tenant", op="equals", values=("acme",), variation="fast"),),
    )
    await store.upsert(bumped, if_match=rev.revision)
    assert (await load_goal_engine_config("acme", store=store)).tick_interval_seconds == 5
    assert (await load_goal_engine_config("omnigent", store=store)).tick_interval_seconds == 30
