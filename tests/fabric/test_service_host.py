from __future__ import annotations

import json

import pytest

from omnigent.fabric.manifest import DEFAULT_FABRIC_MANIFEST
from omnigent.fabric.service_host import (
    NatsServiceEndpoint,
    NatsServiceHost,
    create_required_fabric_service_hosts,
    required_fabric_service_versions,
)


@pytest.mark.asyncio
async def test_service_host_serves_ping_info_and_stats() -> None:
    async def _handler(payload: bytes) -> bytes:
        return payload.upper()

    host = NatsServiceHost(
        name="omnigent.fabric.control",
        version="1.0.0",
        description="Fabric control service",
    )
    host.add_endpoint(
        NatsServiceEndpoint(
            name="preflight",
            subject="omnigent.fabric.control.preflight",
            handler=_handler,
        )
    )

    ping = json.loads(await host.handle_control("$SRV.PING.omnigent.fabric.control"))
    info = json.loads(await host.handle_control("$SRV.INFO.omnigent.fabric.control"))
    stats = json.loads(await host.handle_control("$SRV.STATS.omnigent.fabric.control"))

    assert ping["type"] == "io.nats.micro.v1.ping_response"
    assert ping["name"] == "omnigent.fabric.control"
    assert info["type"] == "io.nats.micro.v1.info_response"
    assert info["endpoints"] == [
        {
            "name": "preflight",
            "subject": "omnigent.fabric.control.preflight",
            "queue_group": "q.omnigent.fabric.control",
            "metadata": {},
        }
    ]
    assert stats["type"] == "io.nats.micro.v1.stats_response"
    assert stats["endpoints"][0]["num_requests"] == 0


@pytest.mark.asyncio
async def test_service_host_tracks_endpoint_stats_and_errors() -> None:
    async def _ok(payload: bytes) -> bytes:
        return payload

    async def _boom(_payload: bytes) -> bytes:
        raise RuntimeError("failed")

    host = NatsServiceHost(name="omnigent.fabric.ops", version="1.0.0")
    host.add_endpoint(NatsServiceEndpoint(name="ok", subject="ops.ok", handler=_ok))
    host.add_endpoint(NatsServiceEndpoint(name="boom", subject="ops.boom", handler=_boom))

    assert await host.handle_endpoint("ops.ok", b"data") == b"data"
    with pytest.raises(RuntimeError, match="failed"):
        await host.handle_endpoint("ops.boom", b"data")

    stats = json.loads(await host.handle_control("$SRV.STATS.omnigent.fabric.ops"))
    by_name = {entry["name"]: entry for entry in stats["endpoints"]}
    assert by_name["ok"]["num_requests"] == 1
    assert by_name["ok"]["num_errors"] == 0
    assert by_name["boom"]["num_requests"] == 1
    assert by_name["boom"]["num_errors"] == 1
    assert by_name["boom"]["last_error"] == "failed"


@pytest.mark.asyncio
async def test_required_fabric_services_are_registered_with_endpoints() -> None:
    hosts = create_required_fabric_service_hosts()

    assert set(hosts) == set(DEFAULT_FABRIC_MANIFEST.required_services)
    versions = required_fabric_service_versions()
    assert versions["omnigent.fabric.control"] == "1.0.0"

    control_info = json.loads(
        await hosts["omnigent.fabric.control"].handle_control(
            "$SRV.INFO.omnigent.fabric.control"
        )
    )
    assert {endpoint["name"] for endpoint in control_info["endpoints"]} == {
        "ready",
        "preflight",
        "discover",
    }

    host_worker_info = json.loads(
        await hosts["omnigent.fabric.host_worker"].handle_control(
            "$SRV.INFO.omnigent.fabric.host_worker"
        )
    )
    assert {endpoint["subject"] for endpoint in host_worker_info["endpoints"]} >= {
        "omnigent.fabric.host_worker.launch",
        "omnigent.fabric.host_worker.workspace",
    }
