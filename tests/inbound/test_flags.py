"""Tests for the inbound feature flags (ADR-0155, BDP-2560)."""
from __future__ import annotations

from dataclasses import replace

from bytedesk_omnigent.inbound.flags import (
    INBOUND_CUTOVER_GOAL_DELIVERY,
    INBOUND_PIPELINE_ENABLED,
    evaluate_inbound_flag,
    seed_inbound_flags,
)
from bytedesk_omnigent.runtime_flags.store import InMemoryRuntimeFlagStore


async def _enable(store, key: str) -> None:
    rev = await store.get_revision(key)
    await store.upsert(replace(rev.definition, default_variation="on"), if_match=rev.revision)


async def test_flags_seed_default_off() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_inbound_flags(store)
    assert await evaluate_inbound_flag(INBOUND_PIPELINE_ENABLED, store=store) is False
    assert await evaluate_inbound_flag(INBOUND_CUTOVER_GOAL_DELIVERY, store=store) is False


async def test_cutover_blocked_until_master_on() -> None:
    store = InMemoryRuntimeFlagStore()
    await seed_inbound_flags(store)
    # turn cutover ON but leave master OFF → prerequisite fails → still off
    await _enable(store, INBOUND_CUTOVER_GOAL_DELIVERY)
    assert await evaluate_inbound_flag(INBOUND_CUTOVER_GOAL_DELIVERY, store=store) is False
    # now turn master ON → cutover fires
    await _enable(store, INBOUND_PIPELINE_ENABLED)
    assert await evaluate_inbound_flag(INBOUND_PIPELINE_ENABLED, store=store) is True
    assert await evaluate_inbound_flag(INBOUND_CUTOVER_GOAL_DELIVERY, store=store) is True


async def test_unknown_flag_fails_closed() -> None:
    store = InMemoryRuntimeFlagStore()
    assert await evaluate_inbound_flag("inbound.nope", store=store) is False
