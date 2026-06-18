"""Signed inbound-webhook / event ingress route (BDP-2249, ADR-0142).

``POST /v1/ingress/{source}`` — verify the HMAC, resolve the binding, deliver to
the durable signal bus. Thin glue over ``omnigent.ingress.process_inbound``
(which holds the tested logic). 404 on no-match / unconfigured source, never 2xx.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def create_ingress_router() -> APIRouter:
    """Build the inbound-webhook ingress router."""
    router = APIRouter()

    @router.post("/ingress/{source}")
    async def receive(source: str, request: Request) -> JSONResponse:
        """Receive a signed external event and deliver it to the signal bus."""
        from omnigent.ingress import (
            default_secret_resolver,
            get_binding_store,
            process_inbound,
        )
        from omnigent.runtime import get_signal_bus

        secret = default_secret_resolver(source)
        if secret is None:
            # Unconfigured source — not a valid ingress target. 404 (never 2xx).
            return JSONResponse(
                {"status": "unknown_source", "detail": f"no secret configured for {source}"},
                status_code=404,
            )
        raw = await request.body()
        provided_sig = request.headers.get(
            "x-omnigent-signature", request.headers.get("x-hub-signature-256", "")
        )
        match_key = request.headers.get("x-omnigent-event", "*")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        result = process_inbound(
            source=source,
            raw_body=raw,
            provided_signature=provided_sig,
            secret=secret,
            store=get_binding_store(),
            bus=get_signal_bus(),
            match_key=match_key,
            payload=payload if isinstance(payload, dict) else None,
        )
        return JSONResponse(
            {
                "status": result.status.value,
                "signal_id": result.signal_id,
                "detail": result.detail,
            },
            status_code=result.http_status,
        )

    return router
