"""Per-user typed event subscription hub (BDP-2394, ADR-0149).

A consumer-facing Publish-Subscribe channel for typed lifecycle Event
Messages (session created, turn completed, sub-agent spawned, …). Producers
:func:`publish` an event under a *user key*; a consumer opens
``GET /v1/events`` and :func:`subscribe`s, optionally narrowing to a set of
event types (EIP Message Filter). This is the cross-cutting event seam that
lets an external system observe omnigent without hand-wiring a callback per
event type (GoF Observer).

Mirrors :mod:`omnigent.runtime.user_session_stream` deliberately: a tiny
fan-out keyed by user key, live-tail only — no replay buffer, no end-of-stream
sentinel. Events published while a user has no stream connected are dropped;
the consumer reconciles via the REST snapshots on (re)connect. Kept free of
``omnigent.runtime`` imports so it can't introduce an import cycle.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterable
from typing import Any

# Subscriber registry: user_key -> set of (queue, event_loop) pairs. The loop
# reference lets a producer on another thread deliver into the queue's owning
# loop via ``call_soon_threadsafe`` (matches user_session_stream).
_subscribers: dict[
    str,
    set[tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]],
] = {}
_lock = threading.Lock()


def publish(user_key: str, event: dict[str, Any]) -> None:
    """
    Broadcast a typed event to every active subscriber for ``user_key``.

    No-op when that user has no stream connected (the common case), so
    producers can fire unconditionally. The event SHOULD carry a ``"type"``
    key so subscribers can filter on it.

    :param user_key: The target user's key — the authenticated user id, or
        the shared single-user sentinel.
    :param event: The event dict, e.g.
        ``{"type": "session.created", "session_id": "conv_abc123"}``.
    """
    with _lock:
        subs = list(_subscribers.get(user_key, ()))
    for queue, loop in subs:
        loop.call_soon_threadsafe(queue.put_nowait, event)


async def subscribe(
    user_key: str,
    *,
    types: Iterable[str] | None = None,
    heartbeat_interval_s: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Subscribe to typed events for ``user_key`` until cancelled.

    Live-tail only — events emitted before this call are not replayed. The
    ``finally`` block always unregisters the slot. Must be called from the
    event loop the caller iterates on.

    :param user_key: The user's key to subscribe under (see :func:`publish`).
    :param types: When set, only events whose ``"type"`` is in this set are
        yielded (EIP Message Filter). ``None`` yields every event.
    :param heartbeat_interval_s: When set, yield a synthetic
        ``{"type": "heartbeat"}`` whenever the queue is idle this long, so an
        SSE socket that has gone half-open is detectable. ``None`` is pure
        event-driven.
    :returns: An async iterator of event dicts.
    """
    type_filter = set(types) if types is not None else None
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    entry = (queue, loop)
    with _lock:
        _subscribers.setdefault(user_key, set()).add(entry)
    try:
        while True:
            if heartbeat_interval_s is None:
                event = await queue.get()
            else:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_s)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
            if type_filter is not None and event.get("type") not in type_filter:
                continue
            yield event
    finally:
        with _lock:
            subs = _subscribers.get(user_key)
            if subs is not None:
                subs.discard(entry)
                if not subs:
                    _subscribers.pop(user_key, None)
