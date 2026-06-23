"""ConfigChangeBus — in-process config.changed pub-sub (BDP-2418)."""

from __future__ import annotations

import asyncio

from omnigent.config import ConfigChange, ConfigChangeBus, config_change_bus


def _change(key: str = "system.x") -> ConfigChange:
    return ConfigChange(key=key, scope="system", etag="2", tier=2, effect_timing="live")


async def test_subscribe_receives_published_change() -> None:
    bus = ConfigChangeBus()
    q = bus.subscribe()
    bus.publish(_change())
    got = await asyncio.wait_for(q.get(), timeout=1.0)
    assert got.key == "system.x"


async def test_publish_fans_out_to_all_subscribers() -> None:
    bus = ConfigChangeBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish(_change("a"))
    assert (await q1.get()).key == "a"
    assert (await q2.get()).key == "a"


def test_unsubscribe_stops_delivery() -> None:
    bus = ConfigChangeBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish(_change())
    assert q.empty()


def test_config_change_bus_is_process_singleton() -> None:
    assert config_change_bus() is config_change_bus()
