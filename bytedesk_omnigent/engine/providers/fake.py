"""In-memory fallback provider (Phase 4, BDP-2586).

So the engine + tests run with NO connected app: :class:`FakeProvider` answers
``evaluate`` / ``execute`` in-process. It is both a test util and the built-in
default that keeps the engine standalone (the provider seam degrades to a no-op
rather than requiring the platform to be up).

A :class:`FakeProvider` registers a :class:`FakeSensor` per declared sensor and a
:class:`FakeActuator` per declared actuator into the engine's existing registries,
so the resolver/actuator paths exercise the *same* code as the remote case — only
the transport differs.
"""
from __future__ import annotations

from typing import Any

from bytedesk_omnigent.engine.providers.contract import ActuatorResult
from bytedesk_omnigent.engine.sensors import SensorContext, SensorReading


class FakeSensor:
    """A sensor whose readings are seeded in-process (no network)."""

    def __init__(self, name: str, *, satisfied: bool = True, value: Any = None) -> None:
        self.name = name
        self._satisfied = satisfied
        self._value = value

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        return {
            "satisfied": self._satisfied,
            "value": self._value if self._value is not None else query,
            "observed_at": ctx.now,
            "stale_after_s": None,
        }


class FakeActuator:
    """An actuator that records calls in-process and returns a canned result."""

    def __init__(self, name: str, *, risk_tier: int = 2, ok: bool = True) -> None:
        self.name = name
        self.risk_tier = risk_tier
        self._ok = ok
        self.calls: list[dict[str, Any]] = []

    async def execute(self, action: dict[str, Any]) -> ActuatorResult:
        self.calls.append(action)
        return ActuatorResult(ok=self._ok, output={"echo": action})


class FakeProvider:
    """An in-process connected-app stand-in for standalone runs + tests."""

    def __init__(
        self,
        name: str = "fake",
        *,
        sensors: dict[str, FakeSensor] | None = None,
        actuators: dict[str, FakeActuator] | None = None,
    ) -> None:
        self.name = name
        self.sensors = sensors or {}
        self.actuators = actuators or {}

    def register_into(self, *, sensor_registry: Any, actuator_registry: Any) -> None:
        """Register this provider's fakes into the engine registries (idempotent)."""
        for s in self.sensors.values():
            if s.name not in sensor_registry.names():
                sensor_registry.register(s.name, lambda s=s: s)
        for a in self.actuators.values():
            if a.name not in actuator_registry.names():
                actuator_registry.register(a.name, lambda a=a: a)


__all__ = ["FakeActuator", "FakeProvider", "FakeSensor"]
