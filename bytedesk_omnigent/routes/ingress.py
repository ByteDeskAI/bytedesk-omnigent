"""Signed inbound-webhook / event ingress route (BDP-2249, ADR-0142).

``POST /v1/ingress/{source}`` — verify the HMAC, resolve the binding, deliver to
the durable signal bus. Thin glue over ``bytedesk_omnigent.ingress.process_inbound``
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
        from bytedesk_omnigent.ingress import (
            get_binding_store,
            process_inbound,
            resolve_secret,
            resolve_webhook_adapter,
        )
        from bytedesk_omnigent.runtime import get_signal_bus

        secret = resolve_secret(source)
        if secret is None:
            # Unconfigured source — not a valid ingress target. 404 (never 2xx).
            return JSONResponse(
                {"status": "unknown_source", "detail": f"no secret configured for {source}"},
                status_code=404,
            )
        raw = await request.body()
        # The per-source adapter owns signature scheme + event-header parsing
        # (BDP-2354); the secret comes from the existing resolver (BDP-2349).
        adapter = resolve_webhook_adapter(source)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None

        # Strangler cutover (ADR-0155, BDP-2566): when the flag is on AND the body
        # is a JSON object, run it through the generic inbound pipeline. A non-dict
        # body can't be an ``ingest()`` ``raw_payload``, so it falls through to the
        # legacy ``process_inbound`` (which tolerates ``payload=None``) unchanged.
        # Signature verification normally lives inside ``process_inbound``; the
        # pipeline has no auth concept, so we verify here first before ingest.
        if isinstance(payload, dict):
            from bytedesk_omnigent.inbound.flags import (
                INBOUND_CUTOVER_SIGNAL_BUS,
                evaluate_inbound_flag,
            )

            if await evaluate_inbound_flag(INBOUND_CUTOVER_SIGNAL_BUS, source=source):
                if not adapter.verify(raw, request.headers, secret):
                    return JSONResponse({"status": "bad_signature"}, status_code=401)

                from bytedesk_omnigent.inbound.pipeline import ingest
                from bytedesk_omnigent.inbound.processors import all_processors
                from bytedesk_omnigent.inbound.store import get_inbound_event_store
                from bytedesk_omnigent.inbound.translators import CHANNEL_SIGNAL

                result = ingest(
                    channel=CHANNEL_SIGNAL,
                    source=source,
                    raw_payload=payload,
                    headers=dict(request.headers),
                    store=get_inbound_event_store(),
                    processors=all_processors(),
                )
                return JSONResponse(
                    {
                        "status": result.status,
                        "idempotencyKey": result.idempotency_key,
                        "eventType": result.event_type,
                        "duplicate": result.duplicate,
                        "detail": result.detail,
                    },
                    status_code=result.http_status,
                )

        result = process_inbound(
            source=source,
            raw_body=raw,
            headers=request.headers,
            secret=secret,
            store=get_binding_store(),
            bus=get_signal_bus(),
            adapter=adapter,
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
