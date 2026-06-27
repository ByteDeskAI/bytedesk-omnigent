"""Condition AST for the Goal Engine — the open, pluggable readiness layer (BDP-2584).

Phase 1 gated goals on a flat list of ``GoalDependency`` rows; this generalizes
that into a boolean tree of :class:`Leaf` conditions, each of which names a
**sensor** (the thing that observes the world) plus a **predicate** over that
sensor's reading. The resolver (``engine.resolver``) evaluates the tree against a
dict of sensor readings and produces ``actionable`` + ``waiting_reasons``.

A :class:`Leaf` keys into the readings dict by ``reading_key()`` — a deterministic
``"{sensor}:{k=v,...}"`` string — so a sensor is evaluated **once per distinct
query** and shared across every leaf that asks the same question.

``Predicate`` is value-based: ``exists`` gates on the reading's ``satisfied``
flag (the legacy "dependency resolved?" question), while ``equals``/``gt``/``lt``/
``contains`` gate on the reading's returned ``value`` so a condition can depend on
*what* a sensor saw, not just that it saw something.

Everything round-trips through ``to_dict``/``from_dict`` so a tree persists inside
``goal.payload`` JSON (no new column — see the resolver docstring).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Reading shape (kept as a plain dict, not a class — it's a wire/JSON record):
#   {"satisfied": bool, "value": Any, "observed_at": int, "stale_after_s": int | None}
Reading = dict[str, Any]
Readings = dict[str, Reading]

PREDICATE_OPS = ("exists", "equals", "gt", "lt", "contains")


@dataclass(frozen=True)
class Predicate:
    """A test applied to a single sensor reading.

    - ``exists`` — true when the reading's ``satisfied`` flag is true (value ignored).
    - ``equals`` / ``gt`` / ``lt`` — compare the reading's ``value`` to ``operand``.
    - ``contains`` — ``operand in value`` (substring for str, membership for lists).
    """

    op: str
    operand: Any = None

    def __post_init__(self) -> None:
        if self.op not in PREDICATE_OPS:
            raise ValueError(
                f"unknown predicate op {self.op!r}; expected one of {list(PREDICATE_OPS)}"
            )

    def test(self, reading: Reading | None) -> bool:
        if reading is None:
            return False
        if self.op == "exists":
            return bool(reading.get("satisfied"))
        value = reading.get("value")
        if self.op == "equals":
            return value == self.operand
        if self.op == "gt":
            return value is not None and value > self.operand
        if self.op == "lt":
            return value is not None and value < self.operand
        if self.op == "contains":
            try:
                return self.operand in value
            except TypeError:
                return False
        return False  # pragma: no cover — guarded by __post_init__

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "operand": self.operand}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Predicate:
        return cls(op=d["op"], operand=d.get("operand"))


@dataclass(frozen=True)
class Leaf:
    """A single ``sensor + query + predicate`` condition."""

    sensor: str
    query: dict[str, Any]
    predicate: Predicate

    def reading_key(self) -> str:
        """Deterministic readings-dict key — one reading per (sensor, query)."""
        parts = ",".join(f"{k}={self.query[k]}" for k in sorted(self.query))
        return f"{self.sensor}:{parts}"

    def eval(self, readings: Readings) -> bool:
        return self.predicate.test(readings.get(self.reading_key()))

    def leaves(self) -> list[Leaf]:
        return [self]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "leaf",
            "sensor": self.sensor,
            "query": self.query,
            "predicate": self.predicate.to_dict(),
        }


@dataclass(frozen=True)
class All:
    """True when every child is true (empty = vacuously true)."""

    nodes: list[ConditionNode] = field(default_factory=list)

    def eval(self, readings: Readings) -> bool:
        return all(n.eval(readings) for n in self.nodes)

    def leaves(self) -> list[Leaf]:
        return [leaf for n in self.nodes for leaf in n.leaves()]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "all", "nodes": [n.to_dict() for n in self.nodes]}


@dataclass(frozen=True)
class Any_:
    """True when at least one child is true (empty = false)."""

    nodes: list[ConditionNode] = field(default_factory=list)

    def eval(self, readings: Readings) -> bool:
        return any(n.eval(readings) for n in self.nodes)

    def leaves(self) -> list[Leaf]:
        return [leaf for n in self.nodes for leaf in n.leaves()]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "any", "nodes": [n.to_dict() for n in self.nodes]}


@dataclass(frozen=True)
class Not:
    """True when the wrapped node is false."""

    node: ConditionNode

    def eval(self, readings: Readings) -> bool:
        return not self.node.eval(readings)

    def leaves(self) -> list[Leaf]:
        return self.node.leaves()

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not", "node": self.node.to_dict()}


# Public name is ``Any`` (the prompt's spelling); ``Any_`` avoids shadowing
# ``typing.Any`` used above in annotations.
Any = Any_

ConditionNode = Leaf | All | Any_ | Not


def from_dict(d: dict[str, Any]) -> ConditionNode:
    """Reconstruct a condition tree from its ``to_dict`` form."""
    kind = d["type"]
    if kind == "leaf":
        return Leaf(
            sensor=d["sensor"],
            query=dict(d.get("query") or {}),
            predicate=Predicate.from_dict(d["predicate"]),
        )
    if kind == "all":
        return All([from_dict(n) for n in d.get("nodes", [])])
    if kind == "any":
        return Any_([from_dict(n) for n in d.get("nodes", [])])
    if kind == "not":
        return Not(from_dict(d["node"]))
    raise ValueError(f"unknown condition node type {kind!r}")


__all__ = [
    "All",
    "Any",
    "ConditionNode",
    "Leaf",
    "Not",
    "Predicate",
    "Reading",
    "Readings",
    "from_dict",
]
