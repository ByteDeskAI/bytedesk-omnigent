"""Internal peer tunnel proxy — executes runner HTTP on the owning replica."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from omnigent.coordination.peer_forward import REPLICA_TARGET_HEADER
from omnigent.coordination.replica_id import server_replica_id
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport

_logger = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def create_peer_tunnel_router(registry: TunnelRegistry) -> APIRouter:
    """Mount the coordination peer tunnel proxy routes.

    :param registry: Local tunnel registry on this replica.
    :returns: FastAPI router for ``/v1/_coord/peer/tunnel/runner/...``.
    """
    router = APIRouter(include_in_schema=False)

    @router.api_route(
        "/v1/_coord/peer/tunnel/runner/{runner_id}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def peer_tunnel_proxy(
        runner_id: str,
        path: str,
        request: Request,
    ) -> Response:
        target = request.headers.get(REPLICA_TARGET_HEADER)
        local = server_replica_id()
        if target and target != local:
            return Response(
                status_code=421,
                content=f"replica mismatch: expected {target!r}, got {local!r}",
            )
        if registry.get(runner_id) is None:
            return Response(status_code=503, content="runner offline on this replica")

        tunnel_path = f"/{path}" if path else "/"
        if request.url.query:
            tunnel_path = f"{tunnel_path}?{request.url.query}"

        headers = [
            (name, value)
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP
            and name.lower() != REPLICA_TARGET_HEADER.lower()
        ]
        body = await request.body()
        tunnel_request = httpx.Request(
            request.method,
            f"http://runner{tunnel_path}",
            headers=headers,
            content=body,
        )
        transport = WSTunnelTransport(registry, runner_id)
        try:
            tunnel_response = await transport.handle_async_request(tunnel_request)
        except httpx.HTTPError as exc:
            _logger.warning(
                "peer tunnel proxy failed for runner=%s path=%s",
                runner_id,
                tunnel_path,
                exc_info=True,
            )
            return Response(status_code=503, content=str(exc))

        response_headers = {
            name: value
            for name, value in tunnel_response.headers.items()
            if name.lower() not in _HOP_BY_HOP
        }

        async def _body_stream() -> bytes:
            async for chunk in tunnel_response.aiter_bytes():
                yield chunk

        return StreamingResponse(
            _body_stream(),
            status_code=tunnel_response.status_code,
            headers=response_headers,
        )

    return router


__all__ = ["create_peer_tunnel_router"]