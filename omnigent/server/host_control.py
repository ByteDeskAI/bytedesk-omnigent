"""Cross-replica host control request/reply.

Host WebSocket tunnels terminate on one server replica, but REST requests can
land on any replica. This module keeps the host tunnel protocol local while
forwarding pre-runner host commands through the active coordination backplane
when the current replica does not own the host connection.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omnigent.coordination.lifecycle import get_active_backplane
from omnigent.coordination.protocol import CoordinationBackplane
from omnigent.host.frames import (
    HostLaunchRunnerFrame,
    HostListDirFrame,
    HostStatFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

_SUBJECT_PREFIX = "omnigent.host_control"


@dataclass
class HostControlError(Exception):
    """Host-control failure that route layers can map to their own errors."""

    status_code: int
    message: str


@dataclass
class HostLaunchControlResult:
    """Result of a ``host.launch_runner`` command."""

    result: dict[str, str | None]
    acked: bool = True


def _subject_token(value: str) -> str:
    """Encode arbitrary ids into one NATS-safe subject token."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _request_subject(host_id: str) -> str:
    """Return the owner-replica request subject for a host id."""
    return f"{_SUBJECT_PREFIX}.host.{_subject_token(host_id)}"


def _reply_subject(backplane: CoordinationBackplane, request_id: str) -> str:
    """Return a unique reply subject for one forwarded command."""
    return (
        f"{_SUBJECT_PREFIX}.reply."
        f"{_subject_token(backplane.replica_id)}.{_subject_token(request_id)}"
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _json_dict(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _await_reply(
    backplane: CoordinationBackplane,
    subject: str,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Wait for the first reply on a temporary subject."""

    async def _first() -> dict[str, Any]:
        async for raw in backplane.subscribe(subject):
            decoded = _json_dict(raw)
            if decoded is not None:
                return decoded
        raise HostControlError(502, "host-control reply subscription closed")

    try:
        return await asyncio.wait_for(_first(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise HostControlError(504, "host-control request timed out") from exc


async def _forward_request(
    *,
    host_id: str,
    kind: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Publish a host-control request to the replica that owns the host tunnel."""
    backplane = get_active_backplane()
    if backplane is None:
        raise HostControlError(409, "host is offline")

    owner = await backplane.resolve_resource("host", host_id)
    if owner is None:
        raise HostControlError(409, "host is offline")

    request_id = secrets.token_hex(8)
    reply = _reply_subject(backplane, request_id)
    reply_task = asyncio.create_task(
        _await_reply(backplane, reply, timeout_s=timeout_s),
        name=f"host-control-reply:{host_id}:{kind}",
    )
    try:
        await asyncio.sleep(0)
        await backplane.publish(
            _request_subject(host_id),
            _json_bytes(
                {
                    "request_id": request_id,
                    "reply": reply,
                    "origin": backplane.replica_id,
                    "host_id": host_id,
                    "kind": kind,
                    "payload": payload,
                }
            ),
        )
        response = await reply_task
    finally:
        if not reply_task.done():
            reply_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reply_task

    if not response.get("ok"):
        status = response.get("status_code")
        detail = response.get("detail")
        raise HostControlError(
            status if isinstance(status, int) else 502,
            detail if isinstance(detail, str) and detail else "host-control request failed",
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise HostControlError(502, "host-control reply missing result")
    return result


async def _local_round_trip(
    *,
    host_registry: HostRegistry,
    conn: HostConnection,
    request_id: str,
    frame: str,
    pending: dict[str, asyncio.Future[dict[str, Any]]],
    timeout_s: float,
    timeout_message: str,
) -> dict[str, Any]:
    """Send one host frame over the local tunnel and await its pending future."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending[request_id] = future
    try:
        if host_registry.get(conn.host_id) is not conn:
            raise HostControlError(409, "host connection was replaced")
        try:
            host_registry.send_text(conn, frame)
        except ConnectionError as exc:
            raise HostControlError(409, "host connection was replaced") from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise HostControlError(504, timeout_message) from exc
    finally:
        pending.pop(request_id, None)


async def _local_stat(
    host_registry: HostRegistry,
    conn: HostConnection,
    *,
    path: str,
    timeout_s: float,
) -> dict[str, Any]:
    request_id = secrets.token_hex(8)
    return await _local_round_trip(
        host_registry=host_registry,
        conn=conn,
        request_id=request_id,
        frame=encode_host_frame(HostStatFrame(request_id=request_id, path=path)),
        pending=conn.pending_stats,
        timeout_s=timeout_s,
        timeout_message=(
            f"host '{conn.host_id}' did not respond to stat within {timeout_s:.0f}s"
        ),
    )


async def _local_list_dir(
    host_registry: HostRegistry,
    conn: HostConnection,
    *,
    path: str,
    limit: int,
    after: str | None,
    before: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    request_id = secrets.token_hex(8)
    return await _local_round_trip(
        host_registry=host_registry,
        conn=conn,
        request_id=request_id,
        frame=encode_host_frame(
            HostListDirFrame(
                request_id=request_id,
                path=path,
                limit=limit,
                after=after,
                before=before,
            )
        ),
        pending=conn.pending_list_dirs,
        timeout_s=timeout_s,
        timeout_message=(
            f"host '{conn.host_id}' did not respond to list_dir within {timeout_s:.0f}s"
        ),
    )


async def _local_launch_runner(
    host_registry: HostRegistry,
    conn: HostConnection,
    *,
    binding_token: str,
    workspace: str,
    harness: str | None,
    timeout_s: float,
) -> HostLaunchControlResult:
    request_id = secrets.token_hex(8)
    try:
        result = await _local_round_trip(
            host_registry=host_registry,
            conn=conn,
            request_id=request_id,
            frame=encode_host_frame(
                HostLaunchRunnerFrame(
                    request_id=request_id,
                    binding_token=binding_token,
                    workspace=workspace,
                    harness=harness,
                )
            ),
            pending=conn.pending_launches,
            timeout_s=timeout_s,
            timeout_message="host did not respond to launch request",
        )
    except HostControlError as exc:
        if exc.status_code == 504:
            return HostLaunchControlResult(
                {"status": "failed", "runner_id": None, "error": exc.message},
                acked=False,
            )
        raise
    return HostLaunchControlResult(
        {
            "status": result.get("status"),
            "runner_id": result.get("runner_id"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
        }
    )


async def request_host_stat(
    *,
    host_registry: HostRegistry,
    host_id: str,
    path: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Stat a path on a host, forwarding through NATS when needed."""
    conn = host_registry.get(host_id)
    if conn is not None:
        return await _local_stat(host_registry, conn, path=path, timeout_s=timeout_s)
    return await _forward_request(
        host_id=host_id,
        kind="stat",
        payload={"path": path, "timeout_s": timeout_s},
        timeout_s=timeout_s + 1.0,
    )


async def request_host_list_dir(
    *,
    host_registry: HostRegistry,
    host_id: str,
    path: str,
    limit: int,
    after: str | None,
    before: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    """List a host directory, forwarding through NATS when needed."""
    conn = host_registry.get(host_id)
    if conn is not None:
        return await _local_list_dir(
            host_registry,
            conn,
            path=path,
            limit=limit,
            after=after,
            before=before,
            timeout_s=timeout_s,
        )
    return await _forward_request(
        host_id=host_id,
        kind="list_dir",
        payload={
            "path": path,
            "limit": limit,
            "after": after,
            "before": before,
            "timeout_s": timeout_s,
        },
        timeout_s=timeout_s + 1.0,
    )


async def request_host_launch_runner(
    *,
    host_registry: HostRegistry,
    host_id: str,
    binding_token: str,
    workspace: str,
    harness: str | None,
    timeout_s: float,
) -> HostLaunchControlResult:
    """Launch a runner on a host, forwarding through NATS when needed."""
    conn = host_registry.get(host_id)
    if conn is not None:
        return await _local_launch_runner(
            host_registry,
            conn,
            binding_token=binding_token,
            workspace=workspace,
            harness=harness,
            timeout_s=timeout_s,
        )
    result = await _forward_request(
        host_id=host_id,
        kind="launch_runner",
        payload={
            "binding_token": binding_token,
            "workspace": workspace,
            "harness": harness,
            "timeout_s": timeout_s,
        },
        timeout_s=timeout_s + 1.0,
    )
    acked = result.pop("acked", True)
    return HostLaunchControlResult(
        {
            "status": result.get("status"),
            "runner_id": result.get("runner_id"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
        },
        acked=bool(acked),
    )


def start_host_control_server(
    host_registry: HostRegistry,
    conn: HostConnection,
) -> asyncio.Task[None] | None:
    """Start serving forwarded host-control requests for one local host tunnel."""
    backplane = get_active_backplane()
    if backplane is None:
        return None
    return asyncio.create_task(
        serve_host_control_requests(host_registry, conn, backplane=backplane),
        name=f"host-control:{conn.host_id}",
    )


async def serve_host_control_requests(
    host_registry: HostRegistry,
    conn: HostConnection,
    *,
    backplane: CoordinationBackplane,
) -> None:
    """Serve forwarded host-control requests for the tunnel-owning replica."""
    subject = _request_subject(conn.host_id)
    try:
        async for raw in backplane.subscribe(subject):
            if host_registry.get(conn.host_id) is not conn:
                return
            request = _json_dict(raw)
            if request is None:
                continue
            await _handle_control_request(
                host_registry=host_registry,
                conn=conn,
                backplane=backplane,
                request=request,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - background subscriber must not crash the tunnel owner
        _logger.warning("host-control subscriber stopped for %s", conn.host_id, exc_info=True)


async def _handle_control_request(
    *,
    host_registry: HostRegistry,
    conn: HostConnection,
    backplane: CoordinationBackplane,
    request: dict[str, Any],
) -> None:
    """Handle one forwarded host-control request and publish its reply."""
    reply = request.get("reply")
    if not isinstance(reply, str) or not reply:
        return
    kind = request.get("kind")
    payload = request.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        await _publish_error(backplane, reply, 400, "invalid host-control request")
        return

    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
        "stat": lambda p: _handle_stat(host_registry, conn, p),
        "list_dir": lambda p: _handle_list_dir(host_registry, conn, p),
        "launch_runner": lambda p: _handle_launch_runner(host_registry, conn, p),
    }
    handler = handlers.get(kind)
    if handler is None:
        await _publish_error(backplane, reply, 400, f"unsupported host-control kind {kind!r}")
        return
    try:
        result = await handler(payload)
    except HostControlError as exc:
        await _publish_error(backplane, reply, exc.status_code, exc.message)
    except Exception:  # noqa: BLE001 - malformed peer requests return 502, not task death
        _logger.warning("host-control request failed for %s", conn.host_id, exc_info=True)
        await _publish_error(backplane, reply, 502, "host-control request failed")
    else:
        await backplane.publish(reply, _json_bytes({"ok": True, "result": result}))


async def _publish_error(
    backplane: CoordinationBackplane,
    reply: str,
    status_code: int,
    detail: str,
) -> None:
    await backplane.publish(
        reply,
        _json_bytes({"ok": False, "status_code": status_code, "detail": detail}),
    )


async def _handle_stat(
    host_registry: HostRegistry,
    conn: HostConnection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    path = payload.get("path")
    timeout_s = payload.get("timeout_s", 5.0)
    if not isinstance(path, str):
        raise HostControlError(400, "path is required")
    return await _local_stat(
        host_registry,
        conn,
        path=path,
        timeout_s=float(timeout_s) if isinstance(timeout_s, (int, float)) else 5.0,
    )


async def _handle_list_dir(
    host_registry: HostRegistry,
    conn: HostConnection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    path = payload.get("path")
    limit = payload.get("limit", 20)
    timeout_s = payload.get("timeout_s", 5.0)
    if not isinstance(path, str):
        raise HostControlError(400, "path is required")
    return await _local_list_dir(
        host_registry,
        conn,
        path=path,
        limit=limit if isinstance(limit, int) else 20,
        after=payload.get("after") if isinstance(payload.get("after"), str) else None,
        before=payload.get("before") if isinstance(payload.get("before"), str) else None,
        timeout_s=float(timeout_s) if isinstance(timeout_s, (int, float)) else 5.0,
    )


async def _handle_launch_runner(
    host_registry: HostRegistry,
    conn: HostConnection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    binding_token = payload.get("binding_token")
    workspace = payload.get("workspace")
    harness = payload.get("harness")
    timeout_s = payload.get("timeout_s", 30.0)
    if not isinstance(binding_token, str) or not binding_token:
        raise HostControlError(400, "binding_token is required")
    if not isinstance(workspace, str) or not workspace:
        raise HostControlError(400, "workspace is required")
    launch = await _local_launch_runner(
        host_registry,
        conn,
        binding_token=binding_token,
        workspace=workspace,
        harness=harness if isinstance(harness, str) else None,
        timeout_s=float(timeout_s) if isinstance(timeout_s, (int, float)) else 30.0,
    )
    if not launch.acked:
        host_registry.evict(conn)
    return {**launch.result, "acked": launch.acked}


__all__ = [
    "HostControlError",
    "HostLaunchControlResult",
    "request_host_launch_runner",
    "request_host_list_dir",
    "request_host_stat",
    "serve_host_control_requests",
    "start_host_control_server",
]
