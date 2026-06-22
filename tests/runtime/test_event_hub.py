"""Tests for the per-user typed event hub (BDP-2394, ADR-0149).

Pins the publish-subscribe contract: live-tail delivery, the type filter
(Message Filter), and the no-subscriber no-op. Mirrors
``test_session_stream`` — drive a subscriber task, ``sleep(0)`` so it
registers, then publish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from omnigent.runtime import event_hub


async def _collect(
    user_key: str, expected: int, *, types: Iterable[str] | None = None
) -> list[dict]:
    out: list[dict] = []
    async for event in event_hub.subscribe(user_key, types=types):
        out.append(event)
        if len(out) >= expected:
            return out
    return out


@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber() -> None:
    task = asyncio.create_task(_collect("u1", 2))
    await asyncio.sleep(0)  # let the subscriber register its slot
    event_hub.publish("u1", {"type": "session.created", "session_id": "c1"})
    event_hub.publish("u1", {"type": "session.created", "session_id": "c2"})
    got = await asyncio.wait_for(task, timeout=2.0)
    assert [e["session_id"] for e in got] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_type_filter_excludes_non_matching() -> None:
    task = asyncio.create_task(_collect("u2", 1, types={"session.created"}))
    await asyncio.sleep(0)
    event_hub.publish("u2", {"type": "other.event", "x": 1})  # filtered out
    event_hub.publish("u2", {"type": "session.created", "session_id": "c9"})  # delivered
    got = await asyncio.wait_for(task, timeout=2.0)
    assert got == [{"type": "session.created", "session_id": "c9"}]


@pytest.mark.asyncio
async def test_publish_without_subscriber_is_silent_noop() -> None:
    # Must not raise when nobody is listening (the common case).
    event_hub.publish("nobody-home", {"type": "session.created", "session_id": "x"})
