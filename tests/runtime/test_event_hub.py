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


@pytest.fixture(autouse=True)
def _clean_event_hub_registry() -> None:
    """Reset the module-global subscriber map between tests."""
    event_hub._subscribers.clear()
    yield
    event_hub._subscribers.clear()


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


@pytest.mark.asyncio
async def test_heartbeat_emitted_when_queue_idle() -> None:
    """Synthetic heartbeats keep half-open SSE sockets detectable."""
    gen = event_hub.subscribe("u_hb", heartbeat_interval_s=0.05)
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert first == {"type": "heartbeat"}
        second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert second == {"type": "heartbeat"}
    finally:
        await gen.aclose()


# ── Cross-replica NATS fan-out: apply_remote_publish (BDP-2621, ADR-0158) ──


@pytest.mark.asyncio
async def test_apply_remote_publish_delivers_to_local_subscriber() -> None:
    """A peer-replica event is delivered to this replica's local subscribers.

    The event hub has no seq/replay concept (by design), so unlike the session
    stream this delivers unconditionally — the listener in coordination
    lifecycle is responsible for dropping this replica's own echo first.
    """
    task = asyncio.create_task(_collect("u_remote", 1))
    await asyncio.sleep(0)  # let the subscriber register its slot
    event_hub.apply_remote_publish("u_remote", {"type": "session.created", "session_id": "cr"})
    got = await asyncio.wait_for(task, timeout=2.0)
    assert got == [{"type": "session.created", "session_id": "cr"}]


@pytest.mark.asyncio
async def test_apply_remote_publish_noop_without_subscriber() -> None:
    """A peer event for a user with no local stream is a silent no-op."""
    # Must not raise when nobody is listening on this replica.
    event_hub.apply_remote_publish("nobody-home", {"type": "session.created", "session_id": "x"})


def test_publish_invokes_fanout_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``publish`` fans the event out to peer replicas.

    Without this, a ``GET /v1/events`` stream on a peer replica would never see
    events emitted by the replica running the turn.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        event_hub,
        "_fanout_remote",
        lambda user_key, event: calls.append((user_key, event)),
    )
    event_hub.publish("u_fr", {"type": "session.created", "session_id": "c1"})
    assert calls == [("u_fr", {"type": "session.created", "session_id": "c1"})]
