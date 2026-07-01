"""Cross-replica session-stream / user-event NATS fan-out (BDP-2621, ADR-0158).

Two independently-connected server replicas share no in-process
``asyncio.Queue`` fan-out, so a browser SSE connection that lands on a
different replica than the one running a session's relay would see only
heartbeats. These tests pin the fix: the existing coordination backplane
(``CoordinationBackplane.publish``/``.subscribe``) carries session-stream and
user-event fan-out to peer replicas, and each replica applies peer messages to
its local subscribers.

* The real-NATS round-trip is skip-gated (``pytest.importorskip("nats")`` plus a
  live connectivity probe) exactly like a deployed ``nats-server``-dependent
  test — it runs when one is reachable and skips cleanly otherwise.
* The listener + producer wiring is exercised against the zero-dependency
  ``InProcessBackplane`` so it always runs, mirroring
  ``tests/coordination/test_pending_sync.py``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from omnigent.coordination import lifecycle as coord_lifecycle
from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.runtime import event_hub, session_stream

_NATS_URL = os.getenv("OMNIGENT_TEST_NATS_URL", "nats://127.0.0.1:4222")


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Isolate the process-global fan-out registries + coordination globals."""
    session_stream._subscribers.clear()
    session_stream._seq.clear()
    session_stream._replay.clear()
    event_hub._subscribers.clear()
    coord_lifecycle.reset_for_tests()
    yield
    session_stream._subscribers.clear()
    session_stream._seq.clear()
    session_stream._replay.clear()
    event_hub._subscribers.clear()
    coord_lifecycle.reset_for_tests()


async def _require_reachable_nats() -> object:
    """Return a started :class:`NatsBackplane` factory input, or skip.

    Skips the test when ``nats-py`` is not installed or no ``nats-server`` is
    reachable (or JetStream is disabled), so the suite stays green in
    environments without the coordination backend.
    """
    pytest.importorskip("nats")
    from omnigent.coordination.nats_backplane import NatsBackplane

    probe = NatsBackplane(_NATS_URL, replica_id="probe")
    try:
        await asyncio.wait_for(probe.start(), timeout=3.0)
    except Exception:  # any failure means "not available here"
        pytest.skip(f"nats-server unavailable at {_NATS_URL} (or JetStream disabled)")
    await probe.stop()
    return NatsBackplane


# ── (1) Real NATS: cross-backplane subject round-trip ────────────────────


@pytest.mark.asyncio
async def test_session_stream_subject_fans_out_across_backplanes() -> None:
    """A publish on ``omnigent.session.stream.<conv>`` reaches a peer backplane.

    Two independently-connected ``NatsBackplane`` instances (distinct replica
    ids) model the 2-replica deployment: instance A publishes the per-conversation
    subject; instance B's wildcard subscribe receives the identical payload.
    """
    NatsBackplane = await _require_reachable_nats()
    bp_a = NatsBackplane(_NATS_URL, replica_id="replica-a")
    bp_b = NatsBackplane(_NATS_URL, replica_id="replica-b")
    await bp_a.start()
    await bp_b.start()
    try:
        received: list[bytes] = []

        async def _consume() -> None:
            async for raw in bp_b.subscribe("omnigent.session.stream.>"):
                received.append(raw)
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.3)  # let the ephemeral subscription establish

        payload = json.dumps(
            {
                "conversation_id": "conv_test",
                "seq": 1,
                "event": {"type": "response.output_text.delta", "delta": "hi"},
                "origin": "replica-a",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await bp_a.publish("omnigent.session.stream.conv_test", payload)

        await asyncio.wait_for(consumer, timeout=3.0)
        assert received == [payload]
        assert json.loads(received[0].decode("utf-8"))["conversation_id"] == "conv_test"
    finally:
        await bp_a.stop()
        await bp_b.stop()


# ── (2) session-stream listener applies peer publishes (InProcess) ───────


@pytest.mark.asyncio
async def test_session_stream_fanout_listener_delivers_peer_publish() -> None:
    """The listener applies a peer session publish to a local subscriber.

    Models the bug's fix path directly: this replica (``replica-b``) holds the
    SSE subscriber; a publish that originated on ``replica-a`` must be delivered
    locally, while this replica's own echo (``origin == replica-b``) is dropped.
    """
    bp = InProcessBackplane("replica-b")
    await bp.start()
    listener = asyncio.create_task(coord_lifecycle._session_stream_fanout_listener(bp))
    await asyncio.sleep(0.1)  # let the listener subscribe

    gen = session_stream.subscribe("conv_peer")
    try:
        fut = asyncio.ensure_future(anext(gen))
        await asyncio.sleep(0)  # register the subscriber slot

        peer = json.dumps(
            {
                "conversation_id": "conv_peer",
                "seq": 1,
                "event": {"type": "e", "i": 1},
                "origin": "replica-a",
            }
        ).encode("utf-8")
        await bp.publish("omnigent.session.stream.conv_peer", peer)
        assert (await asyncio.wait_for(fut, timeout=2.0)) == {"type": "e", "i": 1}

        # This replica's own echo must be ignored (origin == this replica).
        echo = json.dumps(
            {
                "conversation_id": "conv_peer",
                "seq": 2,
                "event": {"type": "e", "i": 2},
                "origin": "replica-b",
            }
        ).encode("utf-8")
        await bp.publish("omnigent.session.stream.conv_peer", echo)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(gen), timeout=0.3)
    finally:
        await gen.aclose()
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
        await bp.stop()


# ── (3) user-event listener applies peer publishes (InProcess) ───────────


@pytest.mark.asyncio
async def test_userevents_fanout_listener_delivers_peer_publish() -> None:
    """The listener applies a peer user event to a local subscriber, skips echo."""
    bp = InProcessBackplane("replica-b")
    await bp.start()
    listener = asyncio.create_task(coord_lifecycle._userevents_fanout_listener(bp))
    await asyncio.sleep(0.1)

    got: list[dict] = []

    async def _drain() -> None:
        async for event in event_hub.subscribe("user_peer"):
            got.append(event)
            return

    consumer = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    try:
        peer = json.dumps(
            {
                "user_key": "user_peer",
                "event": {"type": "session.created", "session_id": "c1"},
                "origin": "replica-a",
            }
        ).encode("utf-8")
        await bp.publish("omnigent.userevents.stream.user_peer", peer)
        await asyncio.wait_for(consumer, timeout=2.0)
        assert got == [{"type": "session.created", "session_id": "c1"}]

        # Own echo dropped: a second subscriber must not receive it.
        second: list[dict] = []

        async def _drain2() -> None:
            async for event in event_hub.subscribe("user_peer"):
                second.append(event)
                return

        consumer2 = asyncio.create_task(_drain2())
        await asyncio.sleep(0)
        echo = json.dumps(
            {
                "user_key": "user_peer",
                "event": {"type": "session.created", "session_id": "c2"},
                "origin": "replica-b",
            }
        ).encode("utf-8")
        await bp.publish("omnigent.userevents.stream.user_peer", echo)
        await asyncio.sleep(0.2)
        assert second == []
        consumer2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer2
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
        await bp.stop()


# ── (4) producer helpers: no-op + envelope shape ─────────────────────────


def test_fanout_producers_are_noop_without_backplane() -> None:
    """Both producers are silent no-ops when no backplane is active."""
    coord_lifecycle.reset_for_tests()
    # Must not raise.
    coord_lifecycle.fanout_session_publish("conv_x", 1, {"type": "e"})
    coord_lifecycle.fanout_userevents_publish("user_x", {"type": "session.created"})


@pytest.mark.asyncio
async def test_fanout_session_publish_envelope_shape() -> None:
    """``fanout_session_publish`` emits the exact cross-replica envelope."""
    bp = InProcessBackplane("replica-x")
    await bp.start()
    coord_lifecycle._backplane = bp
    coord_lifecycle._loop = asyncio.get_running_loop()
    try:
        received: list[dict] = []

        async def _consume() -> None:
            async for raw in bp.subscribe("omnigent.session.stream.>"):
                received.append(json.loads(raw.decode("utf-8")))
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        coord_lifecycle.fanout_session_publish("conv_env", 7, {"type": "e", "i": 1})
        await asyncio.wait_for(consumer, timeout=2.0)
        assert received == [
            {
                "conversation_id": "conv_env",
                "seq": 7,
                "event": {"type": "e", "i": 1},
                "origin": "replica-x",
            }
        ]
    finally:
        await bp.stop()


@pytest.mark.asyncio
async def test_fanout_userevents_publish_envelope_shape() -> None:
    """``fanout_userevents_publish`` emits the exact cross-replica envelope."""
    bp = InProcessBackplane("replica-y")
    await bp.start()
    coord_lifecycle._backplane = bp
    coord_lifecycle._loop = asyncio.get_running_loop()
    try:
        received: list[dict] = []

        async def _consume() -> None:
            async for raw in bp.subscribe("omnigent.userevents.stream.>"):
                received.append(json.loads(raw.decode("utf-8")))
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        coord_lifecycle.fanout_userevents_publish(
            "user_env", {"type": "session.created", "session_id": "c1"}
        )
        await asyncio.wait_for(consumer, timeout=2.0)
        assert received == [
            {
                "user_key": "user_env",
                "event": {"type": "session.created", "session_id": "c1"},
                "origin": "replica-y",
            }
        ]
    finally:
        await bp.stop()
