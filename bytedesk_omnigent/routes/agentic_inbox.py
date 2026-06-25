"""Agentic Inbox webhook route (BDP-2455)."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def create_agentic_inbox_router() -> APIRouter:
    """Build the Agentic Inbox event webhook router."""
    router = APIRouter()

    @router.post("/agentic-inbox/events")
    async def receive_email_event(request: Request) -> JSONResponse:
        """Receive a signed Agentic Inbox ``email.received`` event."""
        from bytedesk_omnigent.agentic_inbox import (
            WEBHOOK_SECRET_ENV,
            AgenticInboxEmailEvent,
            AgenticInboxEventStatus,
            AgenticInboxResolver,
            get_agentic_inbox_event_store,
            process_email_event,
            verify_agentic_inbox_signature,
        )
        from bytedesk_omnigent.sessions import (
            build_self_call_initiator_from_env,
            get_session_initiator,
            set_session_initiator,
        )
        from omnigent.runtime import get_agent_cache, get_agent_store

        secret = os.environ.get(WEBHOOK_SECRET_ENV, "").strip()
        if not secret:
            return JSONResponse(
                {"status": "unconfigured", "detail": f"{WEBHOOK_SECRET_ENV} is not set"},
                status_code=404,
            )
        raw = await request.body()
        if not verify_agentic_inbox_signature(raw, request.headers, secret):
            return JSONResponse(
                {"status": "bad_signature", "detail": "signature mismatch"},
                status_code=401,
            )
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            event = AgenticInboxEmailEvent.from_payload(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"status": "invalid_payload", "detail": str(exc)},
                status_code=422,
            )

        initiator = get_session_initiator()
        if initiator is None:
            initiator = build_self_call_initiator_from_env()
            if initiator is not None:
                set_session_initiator(initiator)
        if initiator is None:
            return JSONResponse(
                {
                    "status": "dispatch_unavailable",
                    "detail": "no SessionInitiator configured",
                },
                status_code=503,
            )

        resolver = AgenticInboxResolver(get_agent_store(), get_agent_cache())
        result = process_email_event(
            event,
            store=get_agentic_inbox_event_store(),
            resolve_agent_id=resolver.resolve_agent_id,
            initiator=initiator,
        )
        status_code = {
            AgenticInboxEventStatus.DISPATCHED: 202,
            AgenticInboxEventStatus.DUPLICATE: 200,
            AgenticInboxEventStatus.DEAD_LETTERED: 202,
            AgenticInboxEventStatus.FAILED: 503,
        }.get(result.status, 202)
        return JSONResponse(
            {
                "status": result.status.value,
                "event_id": result.event_id,
                "agent_id": result.agent_id,
                "session_id": result.session_id,
                "detail": result.detail,
            },
            status_code=status_code,
        )

    return router
