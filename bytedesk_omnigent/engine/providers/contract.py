"""The four connected-app provider role Protocols (Phase 4, BDP-2586).

A connected app (the ByteDesk platform, Phase 5) FEEDs and ACTs FOR the goal
engine without owning goals. It does so through four narrow roles — the engine
stays **domain-blind**: it knows only opaque provider names and these contracts,
never "sales"/"stripe"/"github". ADR-0008 (Strategy + Adapter): each role is a
Protocol so a Remote adapter, an in-process fake, or a future backend all satisfy
the same seam.

The four roles:

- :class:`~bytedesk_omnigent.engine.sensors.Sensor` — READ a fact for a condition
  leaf. Already defined in ``engine/sensors``; re-exported here so the contract is
  one import. A provider's sensors land in the existing ``SensorRegistry``.
- :class:`Actuator` — DO a side-effecting action for the engine (risk-tiered).
- ``OutcomeSource`` — the app PUSHEs realized value/events IN. It is a *sink*, not
  a polled object: it is the canonical inbound ingress + ``treasury.book_outcome``,
  not an object the engine holds. Documented here, implemented by the ingress route
  + the ``OutcomeProcessor`` (``engine/providers/outcome.py``).
- :class:`WebhookTranslator` — turn a raw provider webhook into a canonical
  :class:`InboundEvent`, for the built-in fallback mode (no connected app present).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from bytedesk_omnigent.engine.sensors import Sensor  # re-export: role #1
from bytedesk_omnigent.inbound.event import InboundEvent
from omnigent.kernel.pluggable.registry import PluggableRegistry

# Seam id for the actuator registry (parity with SENSOR_SEAM).
ACTUATOR_SEAM = "goal_actuator"
ACTUATOR_EXTENSION_HOOK = "goal_actuators"


@dataclass(frozen=True)
class ActuatorResult:
    """The outcome of one actuator execution (wire-friendly)."""

    ok: bool
    output: dict[str, Any] | None = None
    detail: str | None = None


@runtime_checkable
class Actuator(Protocol):
    """Performs one side-effecting action for the engine (ADR-0008 Strategy)."""

    name: str
    risk_tier: int

    async def execute(self, action: dict[str, Any]) -> ActuatorResult: ...


@runtime_checkable
class WebhookTranslator(Protocol):
    """Translate a raw provider webhook into a canonical event (Adapter).

    Used by the built-in fallback mode so the engine can receive a provider's
    webhooks directly when no connected app is fronting it. Returns ``None`` for a
    non-actionable payload (the route acks "ignored"), mirroring
    :class:`~bytedesk_omnigent.inbound.translators.InboundTranslator`.
    """

    source: str

    def translate(
        self, raw: dict[str, Any], headers: dict[str, str], *, now: int
    ) -> InboundEvent | None: ...


class ActuatorRegistry(PluggableRegistry):
    """A :class:`PluggableRegistry` pinned to the ``goal_actuator`` seam."""

    def __init__(self, *, default=None) -> None:
        super().__init__(ACTUATOR_SEAM, default=default)

    def discover_actuator_extensions(self) -> None:
        """Register actuators contributed by extensions (Phase 4)."""
        self.discover_extensions(hook=ACTUATOR_EXTENSION_HOOK)


__all__ = [
    "ACTUATOR_EXTENSION_HOOK",
    "ACTUATOR_SEAM",
    "Actuator",
    "ActuatorRegistry",
    "ActuatorResult",
    "Sensor",
    "WebhookTranslator",
]
