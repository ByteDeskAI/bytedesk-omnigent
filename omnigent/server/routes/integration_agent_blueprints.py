"""Read-only routes for integration-driven agent blueprint previews."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from omnigent.server.integration_agent_blueprints import (
    get_integration_agent_blueprint,
    list_integration_agent_services,
    supported_integration_agent_slugs,
)


def create_integration_agent_blueprints_router() -> APIRouter:
    """Build routes that preview service-specific agent creation payloads."""
    router = APIRouter()

    @router.get("/integration-agent-blueprints")
    async def list_blueprints() -> dict[str, Any]:
        """Return ranked third-party service targets for agent creation."""
        services = list_integration_agent_services()
        return {"count": len(services), "services": services}

    @router.get("/integration-agent-blueprints/{service_slug}")
    async def get_blueprint(service_slug: str) -> dict[str, Any]:
        """Return a deterministic agent blueprint for a service slug."""
        blueprint = get_integration_agent_blueprint(service_slug)
        if blueprint is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "integration_agent_blueprint_not_found",
                    "message": f"Unsupported integration agent target: {service_slug}",
                    "supported_slugs": supported_integration_agent_slugs(),
                },
            )
        return blueprint

    return router
