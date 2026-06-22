"""Connected-app OAuth scope review route.

``POST /v1/integration-scope-review`` lets ByteDesk Platform and autonomous
integration setup agents classify requested OAuth scopes before a connected app is
installed. The route is thin glue over the pure, secret-free review compiler.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class ScopeReviewRequest(BaseModel):
    """Request body for an OAuth scope review."""

    service: str = Field(min_length=1)
    requested_scopes: list[str] = Field(default_factory=list)


def create_scope_review_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the connected-app scope review router.

    :param auth_provider: when set (multi-user) the handler requires a valid
        identity; ``None`` (single-user/local-dev) leaves it open.
    """
    router = APIRouter()

    @router.post("/integration-scope-review")
    async def review_scopes(request: Request, body: ScopeReviewRequest) -> JSONResponse:
        """Review OAuth scopes and return risk plus policy recommendations."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_scope_review import review_integration_scopes

        review = review_integration_scopes(
            service=body.service,
            requested_scopes=body.requested_scopes,
        )
        return JSONResponse(
            {
                "service": review.service,
                "requested_scopes": list(review.requested_scopes),
                "approved_scopes": list(review.approved_scopes),
                "high_risk_scopes": list(review.high_risk_scopes),
                "unknown_scopes": list(review.unknown_scopes),
                "risk": review.risk.value,
                "requires_human_approval": review.requires_human_approval,
                "recommendations": list(review.recommendations),
                "policy_recommendations": list(review.policy_recommendations),
            }
        )

    return router
