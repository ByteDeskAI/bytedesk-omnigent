"""Goal sensors — in-process observers that produce condition readings (BDP-2584).

A :class:`Sensor` answers one question about the world for a :class:`Leaf`'s
``query`` and returns a :class:`SensorReading` (``satisfied`` + ``value`` +
freshness). The resolver groups leaves by ``(sensor, query)``, evaluates each
sensor once, and feeds the readings dict to the condition tree.

Only **in-process, testable** built-ins live here — they read the goal store and
the goal's already-stored payload, never the network:

- :class:`GoalOutcomeSensor` — another goal's status/outcome (``"goal X is done"``).
- :class:`TimeSensor` — clock predicates (after a ts / within a window).
- :class:`ManualSensor` — an existing manual dependency's status.
- :class:`DeliverySensor` — a milestone's two-key state from
  ``goal.payload.hierarchy.milestones`` (ADR-0154), read from **stored** delivery
  state, NOT a live jira/github poll.

**DEFERRED to Phase 4:** live external jira/github polling sensors. Those belong
to the connected-app provider contract and arrive via the extension-discovery hook
(``SensorRegistry.discover_sensor_extensions``) — core never reaches the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from bytedesk_omnigent.engine.sensors.registry import SensorRegistry

# A reading is a plain dict (JSON/wire-friendly), matching ``conditions.Reading``:
#   {"satisfied": bool, "value": Any, "observed_at": int, "stale_after_s": int | None}
SensorReading = dict[str, Any]


@dataclass
class SensorContext:
    """Everything a sensor may read — injected, so sensors are pure to test."""

    goal: Any | None
    goal_store: Any
    now: int


def _reading(
    satisfied: bool, value: Any, ctx: SensorContext, stale_after_s: int | None = None
) -> SensorReading:
    return {
        "satisfied": bool(satisfied),
        "value": value,
        "observed_at": ctx.now,
        "stale_after_s": stale_after_s,
    }


@runtime_checkable
class Sensor(Protocol):
    """Observes one fact for a condition leaf's query (ADR-0008 Strategy)."""

    name: str

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading: ...


class GoalOutcomeSensor:
    """Reads another goal's status/outcome via the goal store.

    ``query``: ``{"goal_id": str}``. ``satisfied`` when that goal is ``done``;
    ``value`` is its status string (``None`` if the goal does not exist).
    """

    name = "goal_outcome"

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        goal_id = query.get("goal_id")
        other = ctx.goal_store.get_goal(goal_id=goal_id) if goal_id else None
        if other is None:
            return _reading(False, None, ctx)
        status = str(other.status)
        return _reading(status == "done", status, ctx, stale_after_s=60)


class TimeSensor:
    """Clock predicates against ``ctx.now``.

    ``query``: ``{"after": ts}`` (now >= ts) or ``{"within": [start, end]}``
    (start <= now <= end). ``value`` is ``ctx.now``.
    """

    name = "time"

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        now = ctx.now
        satisfied = True
        if "after" in query:
            satisfied = satisfied and now >= query["after"]
        if "within" in query:
            start, end = query["within"]
            satisfied = satisfied and start <= now <= end
        # ponytail: stale immediately — a clock reading is never reusable next tick.
        return _reading(satisfied, now, ctx, stale_after_s=0)


class ManualSensor:
    """Reads an existing manual dependency's status off the owning goal.

    ``query``: ``{"dep_id": str}``. ``satisfied`` when the dependency status is
    not ``pending`` (mirrors ``_activation_for``); ``value`` is the status string.
    """

    name = "manual"

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        dep_id = query.get("dep_id")
        goal = ctx.goal
        if goal is not None:
            for dep in goal.dependencies:
                if dep.id == dep_id:
                    return _reading(dep.status != "pending", dep.status, ctx)
        return _reading(False, None, ctx)


class DeliverySensor:
    """Reads a milestone's stored two-key state (ADR-0154), no live poll.

    ``query``: ``{"task_key": str}``. Looks up the milestone in
    ``goal.payload.hierarchy.milestones`` by ``taskKey``; ``satisfied`` when its
    ``status`` is ``done``; ``value`` is that status (``None`` if unknown).
    """

    name = "delivery"

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        task_key = query.get("task_key")
        goal = ctx.goal
        payload = getattr(goal, "payload", None) or {}
        milestones = (payload.get("hierarchy") or {}).get("milestones") or []
        for m in milestones:
            if m.get("taskKey") == task_key:
                status = m.get("status")
                return _reading(status == "done", status, ctx)
        return _reading(False, None, ctx)


_BUILTINS: tuple[type, ...] = (GoalOutcomeSensor, TimeSensor, ManualSensor, DeliverySensor)


def build_default_registry(*, discover_extensions: bool = False) -> SensorRegistry:
    """A registry with the in-process built-ins registered.

    ``goal_outcome`` is the registered default. Set ``discover_extensions=True``
    to also pull in extension-contributed sensors (Phase 4) — off by default so
    tests and the CLI never touch the heavyweight extension hub.
    """
    reg = SensorRegistry(default=("goal_outcome", GoalOutcomeSensor))
    for cls in _BUILTINS:
        if cls is GoalOutcomeSensor:
            continue  # already the default
        reg.register(cls.name, cls)
    if discover_extensions:
        reg.discover_sensor_extensions()
    return reg


__all__ = [
    "DeliverySensor",
    "GoalOutcomeSensor",
    "ManualSensor",
    "Sensor",
    "SensorContext",
    "SensorReading",
    "SensorRegistry",
    "TimeSensor",
    "build_default_registry",
]
