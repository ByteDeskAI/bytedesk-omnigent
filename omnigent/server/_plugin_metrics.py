"""First-party plugin — ``omnigent.metrics`` (BDP-2509, Section 9.1 metrics row).

The ``omnigent/server/performance_metrics.py`` subpackage's periodic
metrics-publish loop, expressed as a first-party plugin that registers its
*existing* default provider through the kernel's ``background_tasks`` seam — the
dogfooding argument of Section 9.2: a first-party capability ships through the
same ``OmnigentExtension`` seam a third party would use, proving the seam is
expressive enough to host core.

Section 9.1 row::

    | omnigent/server/performance_metrics.py | omnigent.metrics | background_tasks | kernel only |
    | publish_server_metrics_periodically becomes a background task contributed by this plugin. |

This file is **additive and off the boot path**: it is *not* yet wired into
``create_app`` / the lifespan orchestrator (the Integration phase does that —
Section 9.3). It only has to import cleanly and expose a correct
``background_tasks()`` hook return. Today the same loop is started inline by
``MetricsPublishPhase`` / ``create_app``; this plugin does not move, rewrite, or
disable that — it merely re-exposes the *existing* provider through the seam.

Mirrors the prototype core-plugin style (``prototype/omnigent_demo/core/``): a
plain class decorated with the SDK ``@extension`` whose members are stamped with
seam decorators. The kernel's existing ``discover_extensions`` /
``extension_background_factories`` aggregators consume it identically to a
hand-written :class:`omnigent.kernel.extensions.OmnigentExtension`.

All heavy / domain imports are deferred inside the hook method (NON-NEGOTIABLE
rule 4: keep the kernel domain-free, follow the deferred-import pattern) so
importing this module stays kernel-light and circular-import-safe.
"""

from __future__ import annotations

from omnigent.sdk import background, extension


@extension(name="omnigent.metrics", requires=())
class MetricsExtension:
    """First-party ``omnigent.metrics`` plugin.

    Registers the subpackage's existing default metrics-publish loop into the
    kernel ``background_tasks`` seam. ``@background`` synthesises the
    ``background_tasks()`` Protocol hook → ``[factory() -> Awaitable]``; the
    kernel (``extension_background_factories`` / ``ExtensionBackgroundTasksPhase``)
    calls each factory at lifespan start to obtain the awaitable it supervises
    and cancels at shutdown.
    """

    @background
    async def publish_server_metrics_loop(self) -> None:
        """Periodic server-metrics publish loop (the subpackage default provider).

        Lazily imports ``omnigent.server.performance_metrics`` and reuses its
        *existing* concrete defaults — ``ServerPerformanceMetrics`` (the
        in-memory request tracker) and ``ServerMetricsOtelPublisher`` (the OTel
        snapshot publisher) — then delegates to the existing
        ``publish_server_metrics_periodically`` coroutine. Nothing is moved or
        rewritten; the provider is registered *through* the seam (dogfooding).

        Both default classes construct with all-default arguments, so the loop
        is self-contained — no runtime-injected dependency is required for this
        seam contribution. (When the Integration phase wires this onto the boot
        path it may instead share the ``create_app``-constructed metrics tracker;
        that is out of scope here and intentionally not done.)

        The coroutine runs until cancelled by the lifespan supervisor.
        """
        from omnigent.server.performance_metrics import (
            ServerMetricsOtelPublisher,
            ServerPerformanceMetrics,
            publish_server_metrics_periodically,
        )

        metrics = ServerPerformanceMetrics()
        otel_publisher = ServerMetricsOtelPublisher()
        await publish_server_metrics_periodically(
            metrics,
            otel_publisher=otel_publisher,
        )


def factory() -> MetricsExtension:
    """Entry-point / ``OMNIGENT_EXTENSIONS`` factory for the metrics plugin.

    The irreducible self-registration hook (Section 12.3): a ``pyproject.toml``
    ``[project.entry-points."omnigent.kernel.extensions"]`` row (or an
    ``OMNIGENT_EXTENSIONS=...:factory`` entry) points here, and the kernel's
    existing ``discover_extensions`` calls it to obtain the instance. Wiring
    that entry point is the Integration phase's job; this callable exists so the
    plugin is discoverable the same way every other extension is.
    """
    return MetricsExtension()


__all__ = ["MetricsExtension", "factory"]
