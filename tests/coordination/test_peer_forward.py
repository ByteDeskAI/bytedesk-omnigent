"""Unit tests for cross-replica peer tunnel forwarding."""

from __future__ import annotations

import pytest

from omnigent.coordination.peer_forward import (
    PEER_TUNNEL_PREFIX,
    PeerTunnelTransport,
    peer_base_url,
    peer_tunnel_url,
)


def test_peer_tunnel_url_includes_runner_and_path() -> None:
    url = peer_tunnel_url(
        target_replica="replica-b",
        runner_id="runner_abc",
        path="/v1/sessions/conv_1/events",
    )
    assert url.endswith(
        f"{PEER_TUNNEL_PREFIX}/runner_abc/v1/sessions/conv_1/events"
    )
    assert peer_base_url("replica-b") in url


@pytest.mark.asyncio
async def test_peer_tunnel_transport_refuses_local_loop() -> None:
    import httpx

    from omnigent.coordination import peer_forward as pf

    original = pf.server_replica_id
    pf.server_replica_id = lambda: "replica-a"  # type: ignore[assignment]
    transport = PeerTunnelTransport(target_replica="replica-a", runner_id="runner_1")
    try:
        request = httpx.Request("GET", "http://runner/v1/ping")
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(request)
    finally:
        pf.server_replica_id = original  # type: ignore[assignment]