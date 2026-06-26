"""First-party plugin — ``omnigent.metrics`` (BDP-2509/BDP-2516, Section 9.1 metrics row).

The ``omnigent/server/performance_metrics.py`` subpackage's periodic
metrics-publish loop, expressed as a first-party plugin that registers its
*existing* default provider through the kernel's ``background_tasks`` seam — the
dogfooding argument of Section 9.2: a first-party capability ships through the
same ``OmnigentExtension`` seam a third party would use, proving the seam is
expressive enough to host core.

Section 9.1 row::

    | omnigent/server/performance_metrics.py | omnigent.metrics | background_tasks | kernel only |
    | publish_server_metrics_periodically becomes a background task contributed by this plugin. |

BDP-2516 (Integration, safe slice): this plugin is now the **authoritative**
boot path for the metrics-publish loop — ``create_app`` starts it unconditionally
through :func:`omnigent.core.firstparty_background_task_extensions` (no flag), and
the old inline ``asyncio.create_task(publish_server_metrics_periodically(...))``
in ``_lifespan`` / ``MetricsPublishPhase`` has been removed. To preserve parity
with the inline path — which threaded the ``create_app``-constructed
``server_metrics`` / ``server_metrics_otel`` (the SAME instances the HTTP
middleware records into) — this plugin accepts those instances via constructor
injection. When constructed with no args (the flag-gated discovery shadow / unit
tests) it falls back to fresh subpackage defaults, exactly as before.

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

from typing import TYPE_CHECKING

from omnigent.sdk import background, extension

if TYPE_CHECKING:  # pragma: no cover — typing only, deferred at runtime
    from omnigent.server.performance_metrics import (
        ServerMetricsOtelPublisher,
        ServerPerformanceMetrics,
    )


@extension(name="omnigent.metrics", requires=())
class MetricsExtension:
    """First-party ``omnigent.metrics`` plugin.

    Registers the subpackage's existing default metrics-publish loop into the
    kernel ``background_tasks`` seam. ``@background`` synthesises the
    ``background_tasks()`` Protocol hook → ``[factory() -> Awaitable]``; the
    kernel (``extension_background_factories`` / ``ExtensionBackgroundTasksPhase``)
    calls each factory at lifespan start to obtain the awaitable it supervises
    and cancels at shutdown.

    :param server_metrics: the live request tracker the HTTP middleware records
        into. On the authoritative boot path (BDP-2516) ``create_app`` injects
        the instance it constructed so the published snapshots reflect real
        traffic. ``None`` ⇒ the loop self-constructs a fresh default (the
        flag-gated discovery shadow and unit tests).
    :param server_metrics_otel: the OTel snapshot publisher. Same injection
        contract as ``server_metrics``.
    """

    def __init__(
        self,
        server_metrics: ServerPerformanceMetrics | None = None,
        server_metrics_otel: ServerMetricsOtelPublisher | None = None,
    ) -> None:
        self._server_metrics = server_metrics
        self._server_metrics_otel = server_metrics_otel

    @background
    async def publish_server_metrics_loop(self) -> None:
        """Periodic server-metrics publish loop (the subpackage default provider).

        Delegates to the existing ``publish_server_metrics_periodically``
        coroutine in ``omnigent.server.performance_metrics`` (lazily imported to
        keep this module kernel-light). When ``create_app`` injected the live
        ``server_metrics`` / ``server_metrics_otel`` (BDP-2516 authoritative
        path), those are threaded through so the loop snapshots the SAME tracker
        the HTTP middleware records into. Without injection it falls back to
        fresh subpackage defaults — preserving the prior discovery-shadow
        behaviour.

        The coroutine runs until cancelled by the lifespan supervisor.
        """
        from omnigent.server.performance_metrics import (
            ServerMetricsOtelPublisher,
            ServerPerformanceMetrics,
            publish_server_metrics_periodically,
        )

        metrics = self._server_metrics
        if metrics is None:
            metrics = ServerPerformanceMetrics()
        otel_publisher = self._server_metrics_otel
        if otel_publisher is None:
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
