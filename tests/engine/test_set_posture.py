"""set_autonomy_posture — the command-center arm switch (BDP-2598)."""
from __future__ import annotations

import pytest

from bytedesk_omnigent.engine.config import (
    GOAL_AUTONOMY_POSTURE,
    GOAL_ENGINE_FLAG_DEFINITIONS,
    load_goal_engine_config,
    seed_goal_engine_flags,
    set_autonomy_posture,
)
from bytedesk_omnigent.runtime_flags.store import InMemoryRuntimeFlagStore


async def _seeded() -> InMemoryRuntimeFlagStore:
    store = InMemoryRuntimeFlagStore()
    await seed_goal_engine_flags(store, GOAL_ENGINE_FLAG_DEFINITIONS)
    return store


@pytest.mark.asyncio
async def test_set_global_posture_flips_default(tmp_path) -> None:
    store = await _seeded()
    assert (await load_goal_engine_config(None, store=store)).autonomy_posture == "gated"
    written = await set_autonomy_posture("full_auto", store=store)
    assert written == "full_auto"
    assert (await load_goal_engine_config(None, store=store)).autonomy_posture == "full_auto"


@pytest.mark.asyncio
async def test_set_per_tenant_posture_isolates_other_tenants(tmp_path) -> None:
    store = await _seeded()
    await set_autonomy_posture("full_auto", tenant_id="acme", store=store)
    assert (await load_goal_engine_config("acme", store=store)).autonomy_posture == "full_auto"
    # a different tenant still resolves to the safe global default.
    assert (await load_goal_engine_config("other", store=store)).autonomy_posture == "gated"


@pytest.mark.asyncio
async def test_set_per_tenant_posture_is_idempotent(tmp_path) -> None:
    store = await _seeded()
    await set_autonomy_posture("full_auto", tenant_id="acme", store=store)
    await set_autonomy_posture("gated", tenant_id="acme", store=store)
    flag = (await store.get(GOAL_AUTONOMY_POSTURE))
    tenant_rules = [r for r in flag.rules if r.values == ("acme",)]
    assert len(tenant_rules) == 1  # replaced, not appended
    assert (await load_goal_engine_config("acme", store=store)).autonomy_posture == "gated"


@pytest.mark.asyncio
async def test_invalid_posture_rejected(tmp_path) -> None:
    store = await _seeded()
    with pytest.raises(ValueError):
        await set_autonomy_posture("yolo", store=store)
