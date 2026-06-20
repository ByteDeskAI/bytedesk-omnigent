"""Seam tests for the SnapshotPublisher Protocol (BDP-2349 #52).

Proves: the periodic publish loop is typed against the one-method
:class:`SnapshotPublisher` Protocol, so a non-OTel fake publisher drives the loop
without the concrete OTel publisher; the default OTel publisher still satisfies it.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from omnigent.server.performance_metrics import (
    ServerMetricsOtelPublisher,
    SnapshotPublisher,
    publish_server_metrics_periodically,
)


class _FakeMetrics:
    """Minimal metrics object exposing the ``.snapshot()`` the loop calls."""

    def __init__(self) -> None:
        self.snapshots = 0

    def snapshot(self) -> object:
        self.snapshots += 1
        return object()


class _RecordingPublisher:
    """A non-OTel SnapshotPublisher that just records what it was handed."""

    def __init__(self) -> None:
        self.published: list[object] = []

    def publish(self, snapshot: object) -> None:
        self.published.append(snapshot)


def test_otel_publisher_satisfies_protocol() -> None:
    publisher = ServerMetricsOtelPublisher.__new__(ServerMetricsOtelPublisher)
    assert isinstance(publisher, SnapshotPublisher)


def test_recording_fake_satisfies_protocol() -> None:
    assert isinstance(_RecordingPublisher(), SnapshotPublisher)


@pytest.mark.asyncio
async def test_loop_drives_a_fake_publisher() -> None:
    metrics = _FakeMetrics()
    publisher = _RecordingPublisher()
    task = asyncio.create_task(
        publish_server_metrics_periodically(
            metrics,  # type: ignore[arg-type]
            otel_publisher=publisher,
            interval_seconds=0.01,
        )
    )
    try:
        while not publisher.published:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert publisher.published  # the fake (not the OTel publisher) received snapshots
    assert metrics.snapshots >= 1
