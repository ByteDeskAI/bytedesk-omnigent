"""Parity test for the BDP-2516 authoritative-background-plugin cutover.

After BDP-2516 the ``omnigent.metrics`` and ``omnigent.memory_maintenance``
background loops are no longer started by inline ``asyncio.create_task(...)``
calls in ``create_app`` / ``_lifespan``; they are started — UNCONDITIONALLY, with
no ``OMNIGENT_USE_FIRSTPARTY_PLUGINS`` flag — through the first-party plugin path
``omnigent.core.firstparty_background_task_extensions`` →
``firstparty_background_factories``.

This module pins the parity contract: the authoritative plugin path yields the
SAME background-task factory set the inline path did — exactly the metrics +
memory-maintenance loops, once each, each delegating to the same underlying loop
coroutine the removed inline callsites invoked, with no flag set.

The inline callsites this replaces were (recon, pre-cutover ``_lifespan``)::

    metrics_publish_task = asyncio.create_task(
        publish_server_metrics_periodically(
            server_metrics, otel_publisher=server_metrics_otel,
        )
    )
    memory_maintenance_task = asyncio.create_task(memory_maintenance_loop())
"""

from __future__ import annotations

import os

import pytest


def test_no_firstparty_flag_set_in_env() -> None:
    """The cutover must NOT depend on OMNIGENT_USE_FIRSTPARTY_PLUGINS.

    The whole point of BDP-2516 is that these two slices are authoritative
    regardless of the shadow flag. If the env were truthy here the parity
    assertions below would pass for the wrong reason, so pin that the flag is
    unset/false in the test environment.
    """
    val = os.environ.get("OMNIGENT_USE_FIRSTPARTY_PLUGINS", "")
    assert val.strip().lower() not in {"1", "true", "yes", "on"}


def test_authoritative_path_yields_exactly_metrics_and_memmaint() -> None:
    """The core helper builds exactly the metrics + memory_maintenance plugins.

    Not the full ``default_extensions`` set — only the two background-task
    slices, in a stable order, each contributing exactly one background factory
    (two factories total), matching the two removed inline callsites.
    """
    from omnigent.core import (
        firstparty_background_factories,
        firstparty_background_task_extensions,
    )

    exts = firstparty_background_task_extensions(
        server_metrics=object(),
        server_metrics_otel=object(),
    )
    assert [e.name for e in exts] == [
        "omnigent.metrics",
        "omnigent.memory_maintenance",
    ]

    factories = firstparty_background_factories(exts)
    # One loop each — exactly the two the inline path started, once each.
    assert len(factories) == 2


@pytest.mark.asyncio
async def test_metrics_factory_threads_live_tracker_like_inline_callsite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metrics factory delegates to the SAME loop with the live tracker.

    Parity with the removed inline
    ``publish_server_metrics_periodically(server_metrics, otel_publisher=...)``:
    the authoritative plugin must thread the ``create_app``-constructed
    ``server_metrics`` / ``server_metrics_otel`` (the instances the HTTP
    middleware records into), NOT a freshly-constructed zeroed tracker.
    """
    from omnigent.core import (
        firstparty_background_factories,
        firstparty_background_task_extensions,
    )
    from omnigent.server import performance_metrics as perf

    captured: dict[str, object] = {}

    async def fake_publish(metrics, *, otel_publisher, interval_seconds=10.0):
        captured["metrics"] = metrics
        captured["otel"] = otel_publisher

    monkeypatch.setattr(perf, "publish_server_metrics_periodically", fake_publish)

    live_metrics = perf.ServerPerformanceMetrics()
    live_otel = perf.ServerMetricsOtelPublisher()

    exts = firstparty_background_task_extensions(
        server_metrics=live_metrics,
        server_metrics_otel=live_otel,
    )
    factories = firstparty_background_factories(exts)
    await factories[0]()

    assert captured["metrics"] is live_metrics
    assert captured["otel"] is live_otel


@pytest.mark.asyncio
async def test_memmaint_factory_invokes_loop_zero_arg_like_inline_callsite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory-maintenance factory invokes the loop with ZERO args.

    Byte-equivalent to the removed inline ``memory_maintenance_loop()`` callsite:
    no injected dependency (the loop resolves the memory store at run time).
    """
    from omnigent.core import (
        firstparty_background_factories,
        firstparty_background_task_extensions,
    )
    from omnigent.runtime import memory_maintenance as mm

    called: dict[str, object] = {}

    async def fake_loop(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs

    monkeypatch.setattr(mm, "memory_maintenance_loop", fake_loop)

    exts = firstparty_background_task_extensions()
    factories = firstparty_background_factories(exts)
    # memory_maintenance is the second factory (stable order pinned above).
    await factories[1]()

    assert called["args"] == ()
    assert called["kwargs"] == {}


def test_metrics_and_memmaint_absent_from_flag_gated_shadow_list() -> None:
    """``default_extensions`` (the flag-gated shadow) excludes the two slices.

    They are authoritative now; keeping them in the shadow list too would
    double-register / double-start them when OMNIGENT_USE_FIRSTPARTY_PLUGINS is
    on. The other 11 first-party plugins stay in the shadow list.
    """
    from omnigent.core import default_extensions

    names = [e.name for e in default_extensions()]
    assert "omnigent.metrics" not in names
    assert "omnigent.memory_maintenance" not in names
    # The other slices remain flag-gated shadows.
    assert "omnigent.stores" in names
    assert "omnigent.routes" in names
    assert len(names) == 11
