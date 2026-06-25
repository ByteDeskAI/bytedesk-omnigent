"""Tests for the FastAPI app lifespan hook.

Exercises the ``_lifespan`` context manager in
``omnigent.server.app`` to verify shutdown wiring for the
:class:`TerminalRegistry`. Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md``
§4.4, every live tmux session must be closed when the server's
lifespan exits.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


async def test_lifespan_shutdown_invokes_registry_shutdown(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan exit awaits ``registry.shutdown()``.

    Spies on the registry's ``shutdown`` method and asserts it
    was called exactly once during the lifespan exit. Catches
    the failure mode where the shutdown hook regresses to
    ``pass`` or skips the registry — every long-lived server
    would leak tmux subprocesses on restart.

    What breaks if this fails: deploy hosts accumulate orphan
    tmux sockets across restarts. Each restart adds another
    leaked socket directory. After enough restarts, /tmp fills
    up. We catch this here (in CI, in seconds) instead of in
    production after weeks of restarts.

    Doesn't actually launch any terminals — that requires a
    real tmux subprocess + real spec, which is overkill for
    verifying the *call*. The terminal-side cleanup behavior
    itself is covered by ``tests/terminals/test_registry.py``.
    """
    from omnigent.runtime import get_terminal_registry

    registry = get_terminal_registry()
    real_shutdown = registry.shutdown

    shutdown_calls = 0

    async def spy_shutdown() -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        await real_shutdown()

    monkeypatch.setattr(registry, "shutdown", spy_shutdown)

    async with app.router.lifespan_context(app):
        # Inside the lifespan: shutdown shouldn't have run yet.
        assert shutdown_calls == 0

    # After lifespan exit: shutdown was called exactly once.
    # If 0, the lifespan dropped the call (regression).
    # If >1, something is double-invoking the hook (also wrong).
    assert shutdown_calls == 1, (
        f"Expected registry.shutdown() to be called exactly once on "
        f"lifespan exit, got {shutdown_calls}. If 0, the shutdown "
        f"hook is missing — every server restart will leak any tmux "
        f"sessions registered during the previous lifetime."
    )


async def test_lifespan_starts_periodic_metrics_otel_publisher(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The lifespan starts periodic OTEL publication for server metrics.

    If this wiring regresses, request/resource gauges stop exporting
    even though per-request duration histograms still work.

    BDP-2516: the metrics-publish loop is now started through the
    authoritative ``omnigent.metrics`` first-party plugin (not the removed
    inline ``asyncio.create_task(...)`` in ``_lifespan``). The plugin lazily
    imports ``publish_server_metrics_periodically`` from
    ``omnigent.server.performance_metrics``, so we patch it there. We also
    assert the lifespan threaded the SAME live tracker the HTTP middleware
    records into (``app.state.server_metrics``) — exact parity with the
    removed inline callsite.
    """
    from omnigent.server import performance_metrics as perf
    from omnigent.server.performance_metrics import (
        ServerMetricsOtelPublisher,
        ServerPerformanceMetrics,
    )

    publisher_started = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_publisher(
        metrics: ServerPerformanceMetrics,
        *,
        otel_publisher: ServerMetricsOtelPublisher,
        interval_seconds: float = 10.0,
    ) -> None:
        """
        Capture lifespan publisher arguments and wait for cancellation.

        :param metrics: Metrics tracker owned by the app lifespan.
        :param otel_publisher: OTEL publisher supplied by the app
            lifespan.
        :param interval_seconds: Publisher interval in seconds, e.g.
            ``10.0``.
        """
        assert isinstance(metrics, ServerPerformanceMetrics)
        assert isinstance(otel_publisher, ServerMetricsOtelPublisher)
        assert interval_seconds == 10.0
        captured["metrics"] = metrics
        captured["otel"] = otel_publisher
        publisher_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        perf,
        "publish_server_metrics_periodically",
        fake_publisher,
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(publisher_started.wait(), timeout=1.0)
        # Authoritative path injects the live tracker, not a fresh one.
        assert captured["metrics"] is app.state.server_metrics
        assert captured["otel"] is app.state.server_metrics_otel


async def test_lifespan_starts_memory_maintenance_loop(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan starts the memory-maintenance loop (BDP-2516 authoritative).

    Parity with the removed inline ``memory_maintenance_loop()`` callsite: the
    loop must start on a default boot (no ``OMNIGENT_USE_FIRSTPARTY_PLUGINS``
    flag), invoked with zero args through the authoritative
    ``omnigent.memory_maintenance`` first-party plugin. The plugin lazily imports
    the loop from ``omnigent.runtime.memory_maintenance``, so we patch it there.
    """
    from omnigent.runtime import memory_maintenance as mm

    loop_started = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs
        loop_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mm, "memory_maintenance_loop", fake_loop)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(loop_started.wait(), timeout=1.0)
        # Byte-equivalent to the removed inline zero-arg callsite.
        assert captured["args"] == ()
        assert captured["kwargs"] == {}
