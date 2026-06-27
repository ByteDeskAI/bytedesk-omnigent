"""Sensor registry — the pluggable seam for goal condition observers (BDP-2584).

Built on :class:`omnigent.kernel.pluggable.PluggableRegistry` exactly like the
artifact-store / secret-backend seams: named factories, a registered default, an
``OMNIGENT_USE_GOAL_SENSOR`` override, and an extension-discovery hook so a
connected app can contribute remote sensors in Phase 4 *without* core naming any
provider. Core ships only the in-process built-ins (``goal_outcome`` / ``time`` /
``manual`` / ``delivery``).
"""
from __future__ import annotations

from omnigent.kernel.pluggable.registry import PluggableRegistry

# Stable seam id; also the suffix of the ``OMNIGENT_USE_<SEAM>`` override env var.
SENSOR_SEAM = "goal_sensor"

# Extension hook (mirrors ``BytedeskExtension.secret_backends``): an extension
# returning ``{name: factory}`` from ``goal_sensors()`` contributes sensors.
# DEFERRED to Phase 4 (the connected-app provider contract / remote sensors) —
# core registers the built-ins only and never polls live jira/github here.
SENSOR_EXTENSION_HOOK = "goal_sensors"


class SensorRegistry(PluggableRegistry):
    """A :class:`PluggableRegistry` pinned to the ``goal_sensor`` seam."""

    def __init__(self, *, default=None) -> None:
        super().__init__(SENSOR_SEAM, default=default)

    def discover_sensor_extensions(self) -> None:
        """Register sensors contributed by extensions (Phase 4)."""
        self.discover_extensions(hook=SENSOR_EXTENSION_HOOK)


__all__ = ["SENSOR_EXTENSION_HOOK", "SENSOR_SEAM", "SensorRegistry"]
