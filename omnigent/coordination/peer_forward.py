"""Cross-replica HTTP forwarding for runner tunnel dispatch."""

from __future__ import annotations

import os

import httpx

from omnigent.coordination.replica_id import server_replica_id

REPLICA_TARGET_HEADER = "X-Omnigent-Replica-Target"
PEER_TUNNEL_PREFIX = "/v1/_coord/peer/tunnel/runner"


def peer_base_url(replica_id: str) -> str:
    """Build the HTTP base URL for a peer omnigent-server replica.

    :param replica_id: Pod hostname or explicit ``OMNIGENT_REPLICA_ID``.
    :returns: Base URL without trailing slash.
    """
    template = os.getenv(
        "OMNIGENT_PEER_URL_TEMPLATE",
        "http://{replica_id}.omnigent-server-peer:8000",
    )
    return template.format(replica_id=replica_id).rstrip("/")


def peer_tunnel_url(*, target_replica: str, runner_id: str, path: str) -> str:
    """Compose the peer-internal tunnel proxy URL for a runner request.

    :param target_replica: Owning replica id from coordination resolve.
    :param runner_id: Runner UUID.
    :param path: Original runner-relative path (including query string).
    :returns: Absolute URL on the peer replica.
    """
    base = peer_base_url(target_replica)
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{PEER_TUNNEL_PREFIX}/{runner_id}{path}"


class PeerTunnelTransport(httpx.AsyncBaseTransport):
    """httpx transport that forwards runner HTTP to the owning replica."""

    def __init__(self, *, target_replica: str, runner_id: str) -> None:
        self._target_replica = target_replica
        self._runner_id = runner_id
        self._local_replica = server_replica_id()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._target_replica == self._local_replica:
            raise httpx.ConnectError(
                "peer forward refused: target replica is local but tunnel is absent"
            )
        path = request.url.raw_path.decode("utf-8")
        forward_url = peer_tunnel_url(
            target_replica=self._target_replica,
            runner_id=self._runner_id,
            path=path,
        )
        headers = [
            (key, value)
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        ]
        headers.append((REPLICA_TARGET_HEADER, self._target_replica))
        forward_request = httpx.Request(
            request.method,
            forward_url,
            headers=headers,
            content=await request.aread(),
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, read=None),
        ) as client:
            return await client.send(forward_request, stream=True)


__all__ = [
    "PEER_TUNNEL_PREFIX",
    "REPLICA_TARGET_HEADER",
    "PeerTunnelTransport",
    "peer_base_url",
    "peer_tunnel_url",
]