"""Configuration-Control-Plane read API (ADR-0150, BDP-2415).

The self-describing read surface over the Settings Registry: integrators build
their own config tooling from ``GET /v1/config/descriptors`` (every key + type +
tier + writability) without reading source, and read current values via
``GET /v1/config/values/{key}`` — secrets come back as name + presence only.
Read-only; the write port (BDP-2417) adds PUT/PATCH on the same paths. Gated by
the server's own auth (``auth_provider``); no Office proxy, no platform
capability (the admin lives in omnigent, ADR-0150).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def _serialize(descriptor: object) -> dict:
    d = descriptor
    return {
        "key": d.key,  # type: ignore[attr-defined]
        "scope": d.scope,  # type: ignore[attr-defined]
        "what": d.what,  # type: ignore[attr-defined]
        "json_schema": d.json_schema,  # type: ignore[attr-defined]
        "tier": d.tier,  # type: ignore[attr-defined]
        "sensitivity": d.sensitivity,  # type: ignore[attr-defined]
        "effect_timing": d.effect_timing,  # type: ignore[attr-defined]
        "storage_source": d.storage_source,  # type: ignore[attr-defined]
        "floor": d.floor,  # type: ignore[attr-defined]
        "change_event": d.change_event,  # type: ignore[attr-defined]
        "writable": d.writable,  # type: ignore[attr-defined]
        "read_only_reason": d.read_only_reason,  # type: ignore[attr-defined]
    }


def create_config_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the read-only ``/config`` router (mounted under ``/v1``)."""
    router = APIRouter()

    @router.get("/config/descriptors")
    async def list_descriptors(
        request: Request,
        scope: str | None = Query(None),
        tier: int | None = Query(None),
        writable: bool | None = Query(None),
    ) -> JSONResponse:
        """The self-describing catalog — integrators codegen off this."""
        require_user(request, auth_provider)
        from omnigent.config import build_registry

        descriptors = build_registry().descriptors()
        if scope is not None:
            descriptors = [d for d in descriptors if d.scope == scope]
        if tier is not None:
            descriptors = [d for d in descriptors if d.tier == tier]
        if writable is not None:
            descriptors = [d for d in descriptors if d.writable == writable]
        return JSONResponse({"data": [_serialize(d) for d in descriptors]})

    @router.get("/config/descriptors/{key}")
    async def get_descriptor(request: Request, key: str) -> JSONResponse:
        require_user(request, auth_provider)
        from omnigent.config import build_registry
        from omnigent.errors import ErrorCode, OmnigentError

        descriptor = build_registry().get(key)
        if descriptor is None:
            raise OmnigentError(
                f"no config descriptor for {key!r}", code=ErrorCode.NOT_FOUND
            )
        return JSONResponse(_serialize(descriptor))

    @router.get("/config/values/{key}")
    async def get_value(
        request: Request,
        key: str,
        agent: str | None = Query(None),
        session: str | None = Query(None),
    ) -> JSONResponse:
        """Current value; a secret returns ``{name, present, source}`` only."""
        require_user(request, auth_provider)
        from omnigent.config import ConfigCtx, ConfigNotFoundError, build_registry
        from omnigent.errors import ErrorCode, OmnigentError

        try:
            value = build_registry().read(
                key, ConfigCtx(agent_id=agent, session_id=session)
            )
        except ConfigNotFoundError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.NOT_FOUND) from exc
        return JSONResponse(
            {
                "key": value.key,
                "value": value.value,
                "etag": value.etag,
                "source": value.source,
                "writable": value.writable,
                "read_only_reason": value.read_only_reason,
            }
        )

    return router
