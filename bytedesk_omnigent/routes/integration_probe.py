"""Integration smoke-test probe API route.

``POST /v1/integration-probes/webhook`` compiles a deterministic, signed ingress
probe so operators can validate a webhook binding before enabling a provider.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class WebhookProbeRequest(BaseModel):
    """Request body for compiling a webhook ingress probe."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    match_key: str = Field(min_length=1)
    secret: str = Field(min_length=1)
    payload: dict[str, Any] | None = None
    raw_body: str | None = None
    base_url: str = "http://localhost:8000/v1"


def create_integration_probe_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration probe router.

    The request includes a webhook secret, so multi-user mode requires a valid
    identity. Single-user mode (``auth_provider=None``) remains open like sibling
    ByteDesk local-dev routes.
    """
    router = APIRouter()

    @router.post("/integration-probes/webhook")
    async def webhook_probe(request: Request, body: WebhookProbeRequest) -> JSONResponse:
        """Return a signed, copy/pasteable webhook ingress smoke test."""
        require_user(request, auth_provider)
        if (body.payload is None) == (body.raw_body is None):
            raise HTTPException(
                status_code=422, detail="provide exactly one of payload or raw_body"
            )

        from bytedesk_omnigent.integration_probe import compile_webhook_probe

        probe = compile_webhook_probe(
            source=body.source,
            match_key=body.match_key,
            secret=body.secret,
            payload=body.payload,
            raw_body=body.raw_body,
            base_url=body.base_url,
        )
        return JSONResponse(
            {
                "source": probe.source,
                "match_key": probe.match_key,
                "url": probe.url,
                "body": probe.body,
                "headers": probe.headers,
                "curl_command": probe.curl_command,
                "expected_statuses": probe.expected_statuses,
            }
        )

    return router
