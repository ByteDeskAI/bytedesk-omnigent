"""Inbound-events feed route (ADR-0155, BDP-2563).

The display side of the Wire Tap: ``GET /v1/inbound/recent`` (REST snapshot for
hydration) + ``GET /v1/inbound/events`` (live SSE of inbound-event deltas). Mirrors
the goals SSE route. Both are gated on the ``inbound.feed.enabled`` feature flag —
the feed ramps independently of the pipeline cutovers.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from omnigent.server.routes._auth_helpers import require_user


def _format_sse(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    data = json.dumps(event, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"


def create_inbound_router(auth_provider=None) -> APIRouter:
    """Build the inbound-events feed router (ADR-0155)."""
    router = APIRouter()

    async def _feed_enabled(request: Request) -> bool:
        from bytedesk_omnigent.inbound.flags import (
            INBOUND_FEED_ENABLED,
            evaluate_inbound_flag,
        )

        return await evaluate_inbound_flag(INBOUND_FEED_ENABLED)

    @router.get("/inbound/recent")
    async def recent(request: Request, limit: int = 100) -> JSONResponse:
        """Most-recent inbound events (newest first) for feed hydration."""
        require_user(request, auth_provider)
        if not await _feed_enabled(request):
            return JSONResponse({"events": [], "enabled": False})
        from bytedesk_omnigent.inbound.store import get_inbound_event_store

        events = get_inbound_event_store().recent(limit=min(max(limit, 1), 500))
        return JSONResponse({"events": [asdict(e) for e in events], "enabled": True})

    @router.get("/inbound/events", response_model=None)
    async def subscribe_inbound_events(request: Request) -> StreamingResponse:
        """Subscribe to the live inbound-event feed (SSE)."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.realtime.bridge import INBOUND_EVENT_USER_KEY
        from omnigent.runtime.event_hub import subscribe

        async def _gen() -> AsyncIterator[str]:
            if not await _feed_enabled(request):
                yield _format_sse({"type": "inbound.disabled"})
                return
            async for event in subscribe(
                INBOUND_EVENT_USER_KEY,
                types=("inbound.event",),
                heartbeat_interval_s=20.0,
            ):
                yield _format_sse(event)
                if await request.is_disconnected():
                    break

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
