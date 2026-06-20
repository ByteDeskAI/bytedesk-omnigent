"""Connected-app activation gate route for ByteDesk Platform."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter


def create_integration_activation_gates_router() -> APIRouter:
    """Build the deterministic connected-app activation gate router."""

    router = APIRouter()

    @router.post("/integration-activation-gates/compile")
    async def compile_activation_gate(body: dict[str, Any]) -> dict[str, Any]:
        """Compile a no-side-effect activation decision for a connected app."""

        from bytedesk_omnigent.integration_activation_gates import (
            compile_integration_activation_gate,
        )

        return compile_integration_activation_gate(
            provider=str(body.get("provider", "")),
            workspace_id=str(body.get("workspace_id", "")),
            connected_app_id=str(body.get("connected_app_id", "")),
            capabilities=body.get("capabilities") or None,
            checks=body.get("checks") or None,
        )

    return router
