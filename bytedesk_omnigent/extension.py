"""The single ByteDesk extension (ADR-0143, BDP-2291).

Implements the duck-typed ``omnigent.extensions.OmnigentExtension`` contract
structurally (no import of core needed — it's a Protocol). Phase 1: passthrough +
a ``/v1/_ext/health`` proof route that confirms the entry-point seam discovered and
mounted us. Later phases compose the moved feature submodules' routers here.
"""

from __future__ import annotations

from fastapi import APIRouter


def _health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/_ext/health")
    async def ext_health() -> dict:
        """Phase-1 proof route: the extension seam discovered + mounted us."""
        return {"extension": "bytedesk", "loaded": True}

    return router


class BytedeskExtension:
    """ByteDesk's omnigent extension. Phase 1: passthrough + proof route."""

    name = "bytedesk"

    def routers(self) -> list[APIRouter]:
        return [_health_router()]
