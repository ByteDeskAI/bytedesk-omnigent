"""Pure pub-sub in-process live stream for real-time SSE delivery.

This module is a fan-out broadcaster keyed by ``conversation_id``.
Every active call to :func:`subscribe` owns its own ephemeral
``asyncio.Queue``; :func:`publish` fans the event out to all
queues currently subscribed to that conversation_id. Published events
also enter a bounded per-conversation replay ring so reconnecting SSE
clients can resume with ``Last-Event-ID``. Clients that fall outside
that window recover through ``GET /v1/sessions/{id}`` for the persisted
history and dedupe by item id.

This module owns no per-conversation lifecycle. There is no
``register`` / ``unregister`` step: the first ``subscribe`` call
lazily creates a subscriber slot, and the last ``subscribe`` to
exit removes the slot in its ``finally`` block.

Producer (workflow thread, sync):
    publish(conversation_id, event)  — thread-safe broadcast
    close(conversation_id)           — broadcasts end-of-stream
                                       to all active subscribers

Consumer (SSE endpoint, async):
    subscribe(conversation_id) -> AsyncIterator  — yields events
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from omnigent.runtime import inflight_text, pending_elicitations

_logger = logging.getLogger(__name__)

# Sentinel object that signals end-of-stream to every subscriber.
_DONE = object()

# Subscriber registry: conversation_id -> set of
# (queue, event_loop) pairs. Each queue carries ``(seq, event)`` tuples for
# real published events (``seq`` is the per-conversation monotonic id used for
# Last-Event-ID resume) or the ``_DONE`` sentinel. The event_loop reference is
# needed so the sync producer thread can safely deliver items via
# ``call_soon_threadsafe`` into the queue's owning loop.
_subscribers: dict[
    str,
    set[tuple[asyncio.Queue[tuple[int, dict[str, Any]] | object], asyncio.AbstractEventLoop]],
] = {}
_lock = threading.Lock()

# Bounded per-conversation replay ring for Last-Event-ID resume (BDP-2391,
# ADR-0149). Each entry is ``(seq, event)`` where ``seq`` is a per-conversation
# monotonic counter. A reconnecting subscriber that passes its last seen seq
# gets the buffered suffix replayed before the live tail (EIP Guaranteed
# Delivery + Message Replay). Bounded so a long-lived session can't grow memory
# without limit — a client more than ``_REPLAY_WINDOW`` events behind falls back
# to the snapshot endpoint (the pre-2391 recovery path, still intact).
_REPLAY_WINDOW = 256
_replay: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
_seq: dict[str, int] = {}


def publish(conversation_id: str, event: dict[str, Any]) -> None:
    """
    Broadcast an event to every active subscriber of the given
    conversation (called from sync workflow thread). The event
    payload is delivered verbatim to subscribers. The SSE route carries
    the assigned monotonic sequence number as the event id for
    ``Last-Event-ID`` resume; no ordering field is injected into the
    event payload itself. Events emitted while no subscriber is connected
    are not live-delivered, but they are retained in the bounded replay
    ring for reconnecting clients that present a recent cursor.

    No-op when ``_subscribers`` has no entry for this
    ``conversation_id`` (typical between turns when nothing is
    listening).

    :param conversation_id: The conversation to publish to,
        e.g. ``"conv_abc123"``.
    :param event: The event dict to publish, e.g.
        ``{"type": "response.output_text.delta",
        "delta": "Hello"}``. The ``"type"`` key SHOULD match the
        ``type`` ``Literal`` of one of the variants in
        :data:`omnigent.server.schemas.ServerStreamEvent`;
        the Omnigent route layer validates each emitted dict against
        the union before serializing, so an unmodelled event
        fails loud at the SSE boundary.
    """
    # Track the current turn's streamed assistant text so a client
    # (re)connecting mid-turn can replay it, AND get the verdict
    # on whether this event must be WITHHELD from the live fan-out. The
    # only suppressed events are claude-native ``output_text.delta`` chunks
    # whose message has already committed (a duplicate trailing chunk): the
    # forwarder tails the deltas file separately from the transcript, so a
    # message's last chunk can be POSTed just AFTER its committed item.
    # Computed BEFORE fan-out so we can actually drop it — the old order
    # (fan-out first, record after) could only scrub the reconnect-replay
    # snapshot, never un-send a delta already on a live subscriber's queue.
    # Safe to reorder: ``record_publish`` and the fan-out below run with no
    # ``await`` between them, so within a single ``publish`` call nothing
    # interleaves — the verdict and the enqueue are one atomic step. (This
    # holds for both callers: native deltas, the only suppressible events,
    # arrive on the AP loop via the ``POST /events`` handler; the in-process
    # relay calls ``publish`` from a workflow thread, where ``record_publish``
    # never returns a suppress verdict so the reorder is a no-op there.) The
    # snapshot/live-tail partition is unaffected: a
    # reconnecting client's prefix is still captured by ``subscribe``'s
    # ``pre_ready_snapshot`` at slot registration, independent of this order.
    suppress_live = inflight_text.record_publish(conversation_id, event)
    # Side-channel: keep the cross-session pending-elicitations
    # index in step with the SSE stream. Only acts on
    # ``response.elicitation_request`` events; every other event
    # type is a single dict lookup and a return. A suppressed event is
    # always a text delta, never an elicitation, so this still runs.
    pending_elicitations.record_publish(conversation_id, event)
    if suppress_live:
        return
    with _lock:
        # Assign the per-conversation monotonic seq and record the event in the
        # bounded replay ring (BDP-2391) under the same lock as the fan-out, so
        # the ring order and the delivery order can never diverge.
        # ponytail: per-conversation ring is bounded; the _replay/_seq dicts
        # grow with distinct conversation ids (no eviction) — add LRU if a
        # process ever holds enough live conversations to matter.
        seq = _seq.get(conversation_id, 0) + 1
        _seq[conversation_id] = seq
        _replay.setdefault(conversation_id, deque(maxlen=_REPLAY_WINDOW)).append((seq, event))
        subs = list(_subscribers.get(conversation_id, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(queue.put_nowait, (seq, event))
    # Cross-replica fan-out (BDP-2621, ADR-0158): omnigent-server runs multiple
    # replicas with no sessionAffinity, so a browser's SSE connection can land on
    # a different replica than the one running this session's relay. Mirror the
    # event onto the coordination backplane so a peer replica holding the
    # subscriber can deliver it. Runs AFTER local fan-out and only for events
    # that were NOT suppressed above (suppressed duplicates already returned).
    _fanout_remote(conversation_id, seq, event)


def _fanout_remote(conversation_id: str, seq: int, event: dict[str, Any]) -> None:
    """
    Best-effort mirror of a published event to peer replicas.

    Deferred import of the coordination producer (matches
    :mod:`omnigent.runtime.pending_elicitations`) so this module never imports
    the coordination layer at load time — avoiding any import cycle. A no-op
    when no coordination backplane is active (single-replica / in-process
    posture), and defensively swallows any error so the local publish hot path
    can never be broken by the fan-out side-channel.

    :param conversation_id: Conversation the event was published on.
    :param seq: The per-conversation monotonic seq assigned by :func:`publish`.
    :param event: The event dict, forwarded verbatim.
    """
    try:
        from omnigent.coordination.lifecycle import fanout_session_publish

        fanout_session_publish(conversation_id, seq, event)
    except Exception:  # fan-out must never break local delivery
        _logger.debug(
            "session_stream remote fan-out failed for %s",
            conversation_id,
            exc_info=True,
        )


def apply_remote_publish(conversation_id: str, seq: int, event: dict[str, Any]) -> None:
    """
    Deliver an event that originated on a PEER replica to local subscribers.

    Called ONLY by the coordination session-stream fan-out listener (see
    :func:`omnigent.coordination.lifecycle._session_stream_fanout_listener`),
    never by application code — application code always uses :func:`publish`,
    which both fans out locally and mirrors to peers.

    Delivery + dedup, all under ``_lock`` for the read/compare/update, then the
    thread-safe enqueue outside it (same shape as :func:`publish`):

    * If this replica has no local subscriber for ``conversation_id``, return
      immediately without touching ``_seq``/``_replay`` — a replica must not
      accumulate per-conversation bookkeeping for conversations it never serves.
    * If ``seq <= _seq[conversation_id]`` (the current cursor), drop it as a
      duplicate or stale redelivery. The origin replica's own local
      :func:`publish` always reaches its subscribers before a NATS round-trip
      could echo the same event back, so a same-or-lower seq arriving remotely
      is never new. The rule is "accept any seq STRICTLY GREATER than the
      cursor" — not "exactly cursor + 1" — because this replica may only ever
      observe a subset of a conversation's events, so contiguity is not
      guaranteed and a gap must not wedge the stream.
    * Otherwise advance the cursor, append to the bounded replay ring (so a
      Last-Event-ID reconnect on THIS replica can still resume), and enqueue the
      ``(seq, event)`` tuple onto every local subscriber queue.

    :param conversation_id: Conversation the peer published on.
    :param seq: The peer-assigned per-conversation monotonic seq.
    :param event: The event dict to deliver verbatim.
    """
    with _lock:
        subs = _subscribers.get(conversation_id)
        if not subs:
            return
        if seq <= _seq.get(conversation_id, 0):
            return
        _seq[conversation_id] = seq
        _replay.setdefault(conversation_id, deque(maxlen=_REPLAY_WINDOW)).append((seq, event))
        targets = list(subs)
    for queue, loop in targets:
        loop.call_soon_threadsafe(queue.put_nowait, (seq, event))


def close(conversation_id: str) -> None:
    """
    Broadcast an end-of-stream sentinel to every active subscriber
    of the given conversation. Subscribers awaiting their queue
    will see the sentinel, exit their async-iteration loop, and
    cleanly tear down their entry. Idempotent and a no-op when no
    subscribers are connected.

    :param conversation_id: The conversation whose subscribers
        should be signalled, e.g. ``"conv_abc123"``.
    """
    with _lock:
        subs = list(_subscribers.get(conversation_id, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(queue.put_nowait, _DONE)


async def subscribe(
    conversation_id: str,
    *,
    heartbeat_interval_s: float | None = None,
    ready_event: dict[str, Any] | None = None,
    pre_ready_snapshot: Callable[[], Iterable[dict[str, Any]]] | None = None,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Subscribe to live events for a conversation (event-only, the original
    contract). For Last-Event-ID resume with per-event seq ids, use
    :func:`subscribe_with_ids` — this wrapper drops the seq for the many
    callers/tests that only want the event dicts.
    """
    inner = subscribe_with_ids(
        conversation_id,
        heartbeat_interval_s=heartbeat_interval_s,
        ready_event=ready_event,
        pre_ready_snapshot=pre_ready_snapshot,
        on_subscribed=on_subscribed,
    )
    try:
        async for _seq, event in inner:
            yield event
    finally:
        # Propagate close to the inner generator so its slot-cleanup finally
        # runs when this wrapper is aclose'd (Python 3.13 won't auto-close it).
        await inner.aclose()


async def subscribe_with_ids(
    conversation_id: str,
    *,
    heartbeat_interval_s: float | None = None,
    ready_event: dict[str, Any] | None = None,
    pre_ready_snapshot: Callable[[], Iterable[dict[str, Any]]] | None = None,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
    last_event_id: int | None = None,
) -> AsyncIterator[tuple[int | None, dict[str, Any]]]:
    """
    Subscribe to live events for a conversation, yielding ``(seq, event)``.

    ``seq`` is the per-conversation monotonic id for real published events
    (``None`` for synthetic ready/snapshot/heartbeat frames). When
    ``last_event_id`` is set, the buffered suffix the subscriber missed is
    replayed before the live tail (BDP-2391, Last-Event-ID resume).

    Creates a fresh ephemeral queue for this subscriber, registers
    it under ``conversation_id``, and yields events as they arrive
    from :func:`publish`. Ends when :func:`close` broadcasts the
    end-of-stream sentinel or when the caller stops iterating
    (e.g. client disconnect cancels the generator). The
    ``finally`` block always unregisters this subscriber slot so
    a stale queue cannot keep accumulating events.

    Without ``last_event_id``, this is live-tail only: events emitted
    before this call are NOT replayed. With ``last_event_id``, recent
    buffered events with a higher sequence number are replayed before
    the live tail. Multiple concurrent subscribers to the same
    conversation each see every event independently — there is no
    contention between them.

    Must be called from the asyncio event loop that the caller
    intends to iterate on; the sync producer side uses
    ``loop.call_soon_threadsafe`` to enqueue across threads.

    :param conversation_id: The conversation to subscribe to,
        e.g. ``"conv_abc123"``.
    :param heartbeat_interval_s: When set, yield a synthetic
        ``{"type": "session.heartbeat"}`` dict whenever the queue
        has been idle for this many seconds. Heartbeats are
        generated locally inside this subscriber and never enter
        the publish path, so multiple subscribers each get their
        own independent cadence. ``None`` (default) preserves the
        pure event-driven shape used by harness-internal
        consumers that don't need keepalive.
    :param ready_event: Optional event yielded immediately after
        this subscriber's slot is registered, e.g.
        ``{"type": "session.heartbeat"}``. This gives HTTP/SSE
        clients a subscription acknowledgment before an expensive
        snapshot hook runs, while still registering the live-tail
        queue before any producer can publish a turn event.
    :param pre_ready_snapshot: Optional SYNC hook run once, immediately
        after slot registration and before any ``yield``/``await``. Its
        events are yielded ahead of the live tail. Unlike ``on_subscribed``
        this must be synchronous: it is the only place a dedup-sensitive
        snapshot can be read while still partitioning exactly against the
        live tail, because no ``publish`` can interleave before the first
        suspension. Use it for the in-flight assistant-text replay;
        reading that from ``on_subscribed`` (after ``yield ready_event``)
        double-renders deltas streamed in the gap. Best-effort:
        exceptions are swallowed so a failing snapshot never blocks the
        live tail. ``None`` skips it.
    :param on_subscribed: Optional async hook run once, right after this
        subscriber's slot is registered and before the first live event
        is awaited. Its returned event dicts are yielded as a
        snapshot-on-connect ahead of the live tail. Registering the slot
        first guarantees no delta is dropped between snapshot and tail.
        Best-effort: exceptions are swallowed so a slow/failing snapshot
        never blocks live delivery. ``None`` skips the snapshot.
    :returns: An async iterator of event dicts. Each event is
        yielded verbatim as it was passed to :func:`publish`,
        plus synthetic heartbeat dicts when *heartbeat_interval_s*
        is set.
    """
    queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    entry = (queue, loop)
    with _lock:
        _subscribers.setdefault(conversation_id, set()).add(entry)
        # Capture the Last-Event-ID replay suffix under the SAME lock as
        # registration (BDP-2391): events with a higher seq than the client's
        # last-seen are replayed before the live tail, and because the queue is
        # registered in this same critical section nothing can fall between the
        # replay window and the live queue. Empty when not resuming.
        replay_events: list[tuple[int, dict[str, Any]]] = []
        if last_event_id is not None:
            ring = _replay.get(conversation_id)
            if ring is not None:
                replay_events = [(s, e) for (s, e) in ring if s > last_event_id]
    # Read the pre-ready snapshot synchronously here — after slot
    # registration, before the ``yield`` below suspends. On the Omnigent event
    # loop (where the relay calls ``publish``) nothing runs in between, so
    # the snapshot and the live tail partition exactly: deltas before this
    # point are in the snapshot, deltas after are on ``queue``. Reading it
    # after ``yield ready_event`` instead lets the relay publish deltas
    # into BOTH, which render twice.
    try:
        pre_ready_events: list[dict[str, Any]] = (
            list(pre_ready_snapshot()) if pre_ready_snapshot is not None else []
        )
    except Exception:
        _logger.debug(
            "session_stream pre_ready_snapshot failed for %s",
            conversation_id,
            exc_info=True,
        )
        pre_ready_events = []
    try:
        if ready_event is not None:
            yield (None, ready_event)
        # Replay the missed suffix (BDP-2391) before snapshot/live so a resuming
        # client receives exactly what it missed, each tagged with its seq for
        # the next Last-Event-ID. Synthetic frames (ready/snapshot/heartbeat)
        # carry seq=None and never advance the client's cursor.
        for replay_seq, replay_event in replay_events:
            yield (replay_seq, replay_event)
        for pre_ready_event in pre_ready_events:
            yield (None, pre_ready_event)
        if on_subscribed is not None:
            # Gather the snapshot AFTER the slot is registered (above) so a
            # delta published during the gather lands on ``queue`` and is
            # yielded by the loop below — no missed events between snapshot
            # and live tail (the broker has no buffer). Best-effort: a
            # failing/slow hook must not block the live tail.
            try:
                snapshot_events = await on_subscribed()
            except Exception:
                _logger.debug(
                    "session_stream on_subscribed snapshot failed for %s",
                    conversation_id,
                    exc_info=True,
                )
                snapshot_events = ()
            for snapshot_event in snapshot_events:
                yield (None, snapshot_event)
        while True:
            if heartbeat_interval_s is None:
                item = await queue.get()
            else:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_s)
                except asyncio.TimeoutError:
                    # Queue was idle past the heartbeat deadline. Emit
                    # a synthetic keepalive. Its wire bytes give the
                    # route's ``request.is_disconnected()`` check and
                    # the client's SSE read-timeout something to fire
                    # against if the socket has gone half-open (e.g.
                    # after a laptop sleep).
                    yield (None, {"type": "session.heartbeat"})
                    continue
            if item is _DONE:
                return
            # Live items are (seq, event) tuples assigned by publish.
            live_seq, live_event = item  # type: ignore[misc]
            yield (live_seq, live_event)
    finally:
        with _lock:
            subs = _subscribers.get(conversation_id)
            if subs is not None:
                subs.discard(entry)
                if not subs:
                    _subscribers.pop(conversation_id, None)
