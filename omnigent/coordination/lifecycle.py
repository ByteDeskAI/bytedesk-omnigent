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

_backplane: CoordinationBackplane | None = None
_loop: asyncio.AbstractEventLoop | None = None
_fanout_task: asyncio.Task[None] | None = None


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
    """Connect the active backplane and start the fan-out listener."""
    global _backplane, _loop, _fanout_task
    _loop = asyncio.get_running_loop()
    _backplane = resolve_coordination_backplane()
    await _backplane.start()
    await _hydrate_pending_index(_backplane)
    _fanout_task = asyncio.create_task(_fanout_listener(_backplane))
    return _backplane


async def stop_coordination() -> None:
    """Stop the fan-out listener and disconnect the backplane."""
    global _backplane, _loop, _fanout_task
    if _fanout_task is not None:
        _fanout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _fanout_task
        _fanout_task = None
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


def reset_for_tests() -> None:
    """Clear module globals. Test isolation only."""
    global _backplane, _loop, _fanout_task
    _backplane = None
    _loop = None
    _fanout_task = None


__all__ = [
    "coordination_status",
    "fanout_pending_delete",
    "fanout_pending_upsert",
    "get_active_backplane",
    "reset_for_tests",
    "schedule_backplane",
    "start_coordination",
    "stop_coordination",
]
