"""Unit tests for :mod:`omnigent.runtime.user_session_stream`."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runtime import user_session_stream


@pytest.fixture(autouse=True)
def _clean_user_session_stream_registry() -> None:
    """Reset the module-global subscriber map before and after each test."""
    user_session_stream._subscribers.clear()
    yield
    user_session_stream._subscribers.clear()


async def _collect(user_key: str, expected: int) -> list[dict[str, Any]]:
    """
    Subscribe and collect exactly ``expected`` events, then stop.

    :param user_key: User discovery key to subscribe under.
    :param expected: Number of events to collect before exiting.
    :returns: Collected event dicts in arrival order.
    """
    out: list[dict[str, Any]] = []
    gen = user_session_stream.subscribe(user_key)
    try:
        async for event in gen:
            out.append(event)
            if len(out) >= expected:
                return out
        return out
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_publish_without_subscriber_is_silent_noop() -> None:
    """``publish`` with no subscriber drops events and leaves the registry empty."""
    user_session_stream.publish("user_a", {"type": "session_added", "session_id": "s1"})
    assert user_session_stream._subscribers == {}


@pytest.mark.asyncio
async def test_single_subscriber_receives_events_in_order() -> None:
    """A subscriber gets every event published after it subscribed."""
    task = asyncio.create_task(_collect("user_a", expected=2))
    await asyncio.sleep(0)
    user_session_stream.publish("user_a", {"type": "session_added", "session_id": "s1"})
    user_session_stream.publish("user_a", {"type": "session_added", "session_id": "s2"})
    received = await asyncio.wait_for(task, timeout=2.0)
    assert received == [
        {"type": "session_added", "session_id": "s1"},
        {"type": "session_added", "session_id": "s2"},
    ]


@pytest.mark.asyncio
async def test_pre_subscribe_events_are_lost() -> None:
    """Events published before subscribe are not replayed."""
    user_session_stream.publish("user_b", {"type": "session_added", "session_id": "early"})
    task = asyncio.create_task(_collect("user_b", expected=1))
    await asyncio.sleep(0)
    user_session_stream.publish("user_b", {"type": "session_added", "session_id": "live"})
    received = await asyncio.wait_for(task, timeout=2.0)
    assert received == [{"type": "session_added", "session_id": "live"}]


@pytest.mark.asyncio
async def test_multi_subscriber_fan_out_independently_delivers() -> None:
    """Two subscribers to the same user_key each see every event."""
    t1 = asyncio.create_task(_collect("user_fan", expected=1))
    t2 = asyncio.create_task(_collect("user_fan", expected=1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    user_session_stream.publish("user_fan", {"type": "session_added", "session_id": "s1"})
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
    expected = [{"type": "session_added", "session_id": "s1"}]
    assert r1 == expected
    assert r2 == expected


@pytest.mark.asyncio
async def test_subscriber_slot_cleaned_up_on_exit() -> None:
    """Exiting the subscribe iterator removes the slot from the registry."""
    task = asyncio.create_task(_collect("user_clean", expected=1))
    await asyncio.sleep(0)
    assert "user_clean" in user_session_stream._subscribers
    user_session_stream.publish("user_clean", {"type": "session_added", "session_id": "s1"})
    await asyncio.wait_for(task, timeout=2.0)
    assert "user_clean" not in user_session_stream._subscribers


@pytest.mark.asyncio
async def test_publish_from_different_thread_delivers_via_call_soon_threadsafe() -> None:
    """``publish`` from a worker thread still delivers to the subscriber loop."""
    import threading

    received: list[dict[str, Any]] = []
    ready = threading.Event()

    async def drain() -> None:
        gen = user_session_stream.subscribe("user_thread")
        try:
            async for event in gen:
                received.append(event)
                if len(received) >= 1:
                    return
        finally:
            await gen.aclose()

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)
    ready.set()

    def publish_from_thread() -> None:
        ready.wait(timeout=2.0)
        user_session_stream.publish(
            "user_thread",
            {"type": "session_added", "session_id": "from_thread"},
        )

    thread = threading.Thread(target=publish_from_thread)
    thread.start()
    await asyncio.wait_for(task, timeout=2.0)
    thread.join(timeout=2.0)
    assert received == [{"type": "session_added", "session_id": "from_thread"}]