"""Server lifespan wiring for the coordination backplane."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from omnigent.coordination.factory import resolve_coordination_backplane
from omnigent.coordination.protocol import CoordinationBackplane

_logger = logging.getLogger(__name__)

_FANOUT_SUBJECT = "omnigent.coord.fanout.>"
_PENDING_UPSERT = "pending.upsert"
_PENDING_DELETE = "pending.delete"

# Cross-replica live-stream fan-out (BDP-2621, ADR-0158). The SSE fan-out for
# GET /v1/sessions/{id}/stream and GET /v1/events is otherwise pure in-process,
# so with 2 replicas + no sessionAffinity a browser can land on a different
# replica than the one running the session's relay. These subjects carry the
# published events between replicas over the SAME backplane the pending index
# already uses. Publishers use a per-conversation / per-user subject under the
# prefix; each replica's listener subscribes to the wildcard.
_SESSION_STREAM_PREFIX = "omnigent.session.stream."
_SESSION_STREAM_FANOUT = "omnigent.session.stream.>"
_USEREVENTS_PREFIX = "omnigent.userevents.stream."
_USEREVENTS_FANOUT = "omnigent.userevents.stream.>"

_backplane: CoordinationBackplane | None = None
_loop: asyncio.AbstractEventLoop | None = None
_fanout_task: asyncio.Task[None] | None = None
_session_stream_fanout_task: asyncio.Task[None] | None = None
_userevents_fanout_task: asyncio.Task[None] | None = None


def get_active_backplane() -> CoordinationBackplane | None:
    """Return the started backplane, if any."""
    return _backplane


def coordination_status() -> dict[str, str | bool | None]:
    """Return redacted runtime status for health/capability surfaces."""
    backplane = _backplane
    if backplane is None:
        return {
            "active": False,
            "provider": None,
            "replica_id": None,
        }
    provider = type(backplane).__name__
    if provider == "NatsBackplane":
        provider = "nats"
    elif provider == "InProcessBackplane":
        provider = "inprocess"
    return {
        "active": True,
        "provider": provider,
        "replica_id": backplane.replica_id,
    }


def schedule_backplane(coro: Any) -> None:
    """Schedule a backplane coroutine from sync code (best-effort)."""
    if _backplane is None or _loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        running.create_task(coro)
        return
    future = asyncio.run_coroutine_threadsafe(coro, _loop)

    def _log_failure(fut: asyncio.Future[object]) -> None:
        with contextlib.suppress(Exception):
            exc = fut.exception()
            if exc is not None:
                _logger.debug("coordination backplane task failed", exc_info=exc)

    future.add_done_callback(_log_failure)


async def start_coordination() -> CoordinationBackplane:
    """Connect the active backplane and start the fan-out listeners."""
    global _backplane, _loop, _fanout_task
    global _session_stream_fanout_task, _userevents_fanout_task
    _loop = asyncio.get_running_loop()
    _backplane = resolve_coordination_backplane()
    await _backplane.start()
    await _hydrate_pending_index(_backplane)
    _fanout_task = asyncio.create_task(_fanout_listener(_backplane))
    _session_stream_fanout_task = asyncio.create_task(_session_stream_fanout_listener(_backplane))
    _userevents_fanout_task = asyncio.create_task(_userevents_fanout_listener(_backplane))
    return _backplane


async def stop_coordination() -> None:
    """Stop the fan-out listeners and disconnect the backplane."""
    global _backplane, _loop, _fanout_task
    global _session_stream_fanout_task, _userevents_fanout_task
    for task in (_fanout_task, _session_stream_fanout_task, _userevents_fanout_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _fanout_task = None
    _session_stream_fanout_task = None
    _userevents_fanout_task = None
    if _backplane is not None:
        await _backplane.stop()
    _backplane = None
    _loop = None


def fanout_pending_upsert(
    conversation_id: str,
    elicitation_id: str,
    event: dict[str, Any],
) -> None:
    """Publish a pending-elicitation upsert to peer replicas."""
    if _backplane is None:
        return
    payload = json.dumps(
        {
            "kind": _PENDING_UPSERT,
            "conversation_id": conversation_id,
            "elicitation_id": elicitation_id,
            "event": event,
            "origin": _backplane.replica_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    schedule_backplane(_backplane.publish(f"omnigent.coord.fanout.{_PENDING_UPSERT}", payload))


def fanout_pending_delete(conversation_id: str, elicitation_id: str) -> None:
    """Publish a pending-elicitation delete to peer replicas."""
    if _backplane is None:
        return
    payload = json.dumps(
        {
            "kind": _PENDING_DELETE,
            "conversation_id": conversation_id,
            "elicitation_id": elicitation_id,
            "origin": _backplane.replica_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    schedule_backplane(_backplane.publish(f"omnigent.coord.fanout.{_PENDING_DELETE}", payload))


def fanout_session_publish(conversation_id: str, seq: int, event: dict[str, Any]) -> None:
    """Mirror a session-stream event to peer replicas (BDP-2621, ADR-0158).

    No-op when no backplane is active (single-replica / in-process posture).
    Encodes an envelope carrying the origin replica id so a peer's listener can
    drop this replica's own echo, and publishes to the per-conversation subject.
    """
    if _backplane is None:
        return
    payload = json.dumps(
        {
            "conversation_id": conversation_id,
            "seq": seq,
            "event": event,
            "origin": _backplane.replica_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    schedule_backplane(_backplane.publish(f"{_SESSION_STREAM_PREFIX}{conversation_id}", payload))


def fanout_userevents_publish(user_key: str, event: dict[str, Any]) -> None:
    """Mirror a per-user typed event to peer replicas (BDP-2621, ADR-0158).

    No-op when no backplane is active. The user-event hub has no seq/replay
    concept, so the envelope carries only the user key, the event, and the
    origin replica id (for echo suppression), published to the per-user subject.
    """
    if _backplane is None:
        return
    payload = json.dumps(
        {
            "user_key": user_key,
            "event": event,
            "origin": _backplane.replica_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    schedule_backplane(_backplane.publish(f"{_USEREVENTS_PREFIX}{user_key}", payload))


async def _hydrate_pending_index(backplane: CoordinationBackplane) -> None:
    """Restore pending elicitation hot state from the durable pending KV index."""
    from omnigent.runtime import pending_elicitations as pe

    try:
        records = await backplane.index_list_prefix("pending", "")
    except Exception:  # noqa: BLE001 — recovery failure must not block boot
        _logger.warning("coordination pending-index hydration failed", exc_info=True)
        return
    hydrated = pe.hydrate_from_backplane_records(records)
    if hydrated:
        _logger.info("hydrated %s pending elicitations from coordination backplane", hydrated)


async def _fanout_listener(backplane: CoordinationBackplane) -> None:
    """Apply cross-replica pending sync messages on this replica."""
    from omnigent.runtime import pending_elicitations as pe

    try:
        async for raw in backplane.subscribe(_FANOUT_SUBJECT):
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            origin = msg.get("origin")
            if origin == backplane.replica_id:
                continue
            kind = msg.get("kind")
            conversation_id = msg.get("conversation_id")
            elicitation_id = msg.get("elicitation_id")
            if (
                not isinstance(conversation_id, str)
                or not conversation_id
                or not isinstance(elicitation_id, str)
                or not elicitation_id
            ):
                continue
            if kind == _PENDING_UPSERT:
                event = msg.get("event")
                if isinstance(event, dict):
                    pe.apply_remote_upsert(conversation_id, elicitation_id, event)
            elif kind == _PENDING_DELETE:
                pe.apply_remote_delete(conversation_id, elicitation_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — listener must not take down the server
        _logger.warning("coordination fan-out listener stopped", exc_info=True)


async def _session_stream_fanout_listener(backplane: CoordinationBackplane) -> None:
    """Apply peer-replica session-stream events to this replica's subscribers.

    Subscribes to the per-conversation fan-out wildcard, skips this replica's
    own echo (``origin == replica_id``), and hands each validated event to
    :func:`omnigent.runtime.session_stream.apply_remote_publish` (which itself
    dedups by seq and no-ops when this replica has no local subscriber).
    """
    from omnigent.runtime import session_stream

    try:
        async for raw in backplane.subscribe(_SESSION_STREAM_FANOUT):
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("origin") == backplane.replica_id:
                continue
            conversation_id = msg.get("conversation_id")
            seq = msg.get("seq")
            event = msg.get("event")
            if (
                not isinstance(conversation_id, str)
                or not conversation_id
                or not isinstance(seq, int)
                or isinstance(seq, bool)
                or not isinstance(event, dict)
            ):
                continue
            session_stream.apply_remote_publish(conversation_id, seq, event)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — listener must not take down the server
        _logger.warning("session-stream fan-out listener stopped", exc_info=True)


async def _userevents_fanout_listener(backplane: CoordinationBackplane) -> None:
    """Apply peer-replica user events to this replica's subscribers.

    Subscribes to the per-user fan-out wildcard, skips this replica's own echo
    (``origin == replica_id``), and hands each validated event to
    :func:`omnigent.runtime.event_hub.apply_remote_publish`. Echo suppression
    MUST happen here (before the call), because ``apply_remote_publish``
    delivers unconditionally and never re-mirrors.
    """
    from omnigent.runtime import event_hub

    try:
        async for raw in backplane.subscribe(_USEREVENTS_FANOUT):
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("origin") == backplane.replica_id:
                continue
            user_key = msg.get("user_key")
            event = msg.get("event")
            if not isinstance(user_key, str) or not user_key or not isinstance(event, dict):
                continue
            event_hub.apply_remote_publish(user_key, event)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — listener must not take down the server
        _logger.warning("userevents fan-out listener stopped", exc_info=True)


def reset_for_tests() -> None:
    """Clear module globals. Test isolation only."""
    global _backplane, _loop, _fanout_task
    global _session_stream_fanout_task, _userevents_fanout_task
    _backplane = None
    _loop = None
    _fanout_task = None
    _session_stream_fanout_task = None
    _userevents_fanout_task = None


__all__ = [
    "coordination_status",
    "fanout_pending_delete",
    "fanout_pending_upsert",
    "fanout_session_publish",
    "fanout_userevents_publish",
    "get_active_backplane",
    "reset_for_tests",
    "schedule_backplane",
    "start_coordination",
    "stop_coordination",
]
