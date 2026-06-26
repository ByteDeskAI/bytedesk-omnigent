"""Tests for NATS/backplane-backed host control."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from omnigent.coordination import lifecycle as coord_lifecycle
from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostListDirFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.host_control import (
    request_host_launch_runner,
    request_host_list_dir,
    request_host_stat,
    serve_host_control_requests,
)
from omnigent.server.host_registry import HostRegistry

pytestmark = pytest.mark.asyncio


class _FakeWebSocket:
    async def send_text(self, data: str) -> None:
        del data

    async def receive_text(self) -> str:
        return ""


@pytest.fixture()
async def active_backplane() -> AsyncIterator[InProcessBackplane]:
    """Install a shared in-process backplane as the active coordination seam."""
    coord_lifecycle.reset_for_tests()
    bp = InProcessBackplane("replica-test")
    await bp.start()
    coord_lifecycle._backplane = bp
    coord_lifecycle._loop = asyncio.get_running_loop()
    try:
        yield bp
    finally:
        coord_lifecycle.reset_for_tests()
        await bp.stop()


async def test_remote_registry_can_stat_list_and_launch_host(
    active_backplane: InProcessBackplane,
) -> None:
    """Commands routed through an empty registry reach the owner registry."""
    host_id = "host_remote_control"
    owner_registry = HostRegistry()
    caller_registry = HostRegistry()
    conn = owner_registry.register(
        host_id,
        _FakeWebSocket(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="owner-host"),
        owner="local",
    )
    await active_backplane.claim_resource("host", host_id)
    server_task = asyncio.create_task(
        serve_host_control_requests(owner_registry, conn, backplane=active_backplane)
    )
    await asyncio.sleep(0.01)

    async def _host_daemon() -> None:
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostStatFrame):
                fut = conn.pending_stats.get(frame.request_id)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "exists": True,
                            "type": "directory",
                            "canonical_path": frame.path,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostListDirFrame):
                fut = conn.pending_list_dirs.get(frame.request_id)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "entries": [
                                {
                                    "name": "project",
                                    "path": "/home/local/project",
                                    "type": "directory",
                                    "bytes": None,
                                    "modified_at": 123,
                                }
                            ],
                            "has_more": False,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostLaunchRunnerFrame):
                fut = conn.pending_launches.get(frame.request_id)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "launched",
                            "runner_id": "runner_token_remote",
                            "error": None,
                            "error_code": None,
                        }
                    )

    daemon_task = asyncio.create_task(_host_daemon())
    try:
        stat = await request_host_stat(
            host_registry=caller_registry,
            host_id=host_id,
            path="/home/local",
            timeout_s=1.0,
        )
        assert stat["canonical_path"] == "/home/local"

        listing = await request_host_list_dir(
            host_registry=caller_registry,
            host_id=host_id,
            path="~",
            limit=20,
            after=None,
            before=None,
            timeout_s=1.0,
        )
        assert listing["entries"][0]["name"] == "project"

        launch = await request_host_launch_runner(
            host_registry=caller_registry,
            host_id=host_id,
            binding_token="binding-secret",
            workspace="/home/local/project",
            harness=None,
            timeout_s=1.0,
        )
        assert launch.acked is True
        assert launch.result["runner_id"] == "runner_token_remote"
    finally:
        conn.outbound_queue.put_nowait(None)
        daemon_task.cancel()
        server_task.cancel()
        await asyncio.gather(daemon_task, server_task, return_exceptions=True)
