"""Signed inbound-webhook / event ingress route (BDP-2249, ADR-0142).

``POST /v1/ingress/{source}`` — verify the HMAC, resolve the binding, deliver to
the durable signal bus. Thin glue over ``bytedesk_omnigent.ingress.process_inbound``
(which holds the tested logic). 404 on no-match / unconfigured source, never 2xx.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_ingress_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the inbound-webhook ingress router.

    The inbound delivery endpoint remains unauthenticated because its trust
    boundary is the per-source webhook signature. Binding management is an
    operator surface, so it requires the configured Omnigent user in multi-user
    mode while staying open for local single-user deployments.
    """
    router = APIRouter()

    @router.get("/ingress-bindings")
    async def list_bindings(
        request: Request,
        source: str | None = None,
        enabled: bool | None = None,
    ) -> JSONResponse:
        """List registered webhook-to-signal bindings for integration setup UIs."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.ingress import get_binding_store

        bindings = get_binding_store().list_bindings(source=source, enabled=enabled)
        return JSONResponse({"bindings": [asdict(binding) for binding in bindings]})

    @router.post("/ingress-bindings")
    async def register_binding(request: Request) -> JSONResponse:
        """Create/update a webhook-to-signal binding idempotently.

        Body: ``{"source": "github", "match_key": "pull_request", "signal_id": "..."}``.
        ``match_key`` defaults to ``"*"`` for per-source catch-all bindings.
        """
        require_user(request, auth_provider)
        from bytedesk_omnigent.ingress import get_binding_store

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"status": "invalid_request", "detail": "expected JSON body"},
                status_code=400,
            )
        source = str(body.get("source") or "").strip()
        match_key = str(body.get("match_key") or "*").strip() or "*"
        signal_id = str(body.get("signal_id") or "").strip()
        if not source or not signal_id:
            return JSONResponse(
                {
                    "status": "invalid_request",
                    "detail": "source and signal_id are required",
                },
                status_code=400,
            )

        binding = get_binding_store().register_binding(
            source=source, match_key=match_key, signal_id=signal_id
        )
        return JSONResponse({"binding": asdict(binding)}, status_code=201)

    @router.post("/ingress/{source}/preview")
    async def preview(source: str, request: Request) -> JSONResponse:
        """Verify and resolve a signed event without delivering it."""
        from bytedesk_omnigent.ingress import (
            get_binding_store,
            preview_inbound,
            resolve_secret,
            resolve_webhook_adapter,
        )

        secret = resolve_secret(source)
        if secret is None:
            return JSONResponse(
                {"status": "unknown_source", "detail": f"no secret configured for {source}"},
                status_code=404,
            )
        raw = await request.body()
        result = preview_inbound(
            source=source,
            raw_body=raw,
            headers=request.headers,
            secret=secret,
            store=get_binding_store(),
            adapter=resolve_webhook_adapter(source),
        )
        return JSONResponse(
            {
                "status": result.status.value,
                "signal_id": result.signal_id,
                "detail": result.detail,
            },
            status_code=result.http_status,
        )
    @router.get("/ingress/adapters")
    async def adapters() -> dict[str, object]:
        """Expose setup-safe webhook adapter metadata for integration UIs."""
        from bytedesk_omnigent.ingress import describe_webhook_adapters

        return {"adapters": describe_webhook_adapters()}

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
        response = {
            "status": result.status.value,
            "signal_id": result.signal_id,
            "detail": result.detail,
        }
        if result.escalation is not None:
            response["escalation"] = result.escalation
        return JSONResponse(response, status_code=result.http_status)

    return router
