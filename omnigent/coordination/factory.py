"""Coordination backplane registry and resolution."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.coordination.replica_id import server_replica_id
from omnigent.kernel.pluggable.registry import PluggableRegistry

if TYPE_CHECKING:
    from omnigent.coordination.protocol import CoordinationBackplane

_REGISTRY: PluggableRegistry[Any] | None = None

_SEAM = "coordination_backplane"


def _inprocess_factory() -> CoordinationBackplane:
    return InProcessBackplane(server_replica_id())


def _nats_factory() -> CoordinationBackplane:
    from omnigent.coordination.nats_backplane import NatsBackplane

    url = os.getenv("OMNIGENT_NATS_URL", "").strip()
    if not url:
        raise RuntimeError(
            "OMNIGENT_NATS_URL is required when coordination_backplane=nats"
        )
    return NatsBackplane(url, replica_id=server_replica_id())


def get_coordination_registry() -> PluggableRegistry[Any]:
    """Return the coordination backplane registry (built once per process)."""
    global _REGISTRY
    if _REGISTRY is None:
        registry: PluggableRegistry[Any] = PluggableRegistry(
            _SEAM,
            default=("inprocess", _inprocess_factory),
        )
        registry.register("nats", _nats_factory)
        _REGISTRY = registry
    return _REGISTRY


def resolve_coordination_backplane() -> CoordinationBackplane:
    """Resolve the active coordination backplane for this process.

    When ``OMNIGENT_NATS_URL`` is set and ``OMNIGENT_USE_COORDINATION_BACKPLANE``
    is unset, ``nats`` is selected automatically. An explicit override env always
    wins.
    """
    registry = get_coordination_registry()
    override = os.getenv(f"OMNIGENT_USE_{_SEAM.upper()}", "").strip()
    if override:
        return registry.get(override)
    nats_url = os.getenv("OMNIGENT_NATS_URL", "").strip()
    if nats_url:
        return registry.get("nats")
    return registry.resolve_default()


__all__ = ["get_coordination_registry", "resolve_coordination_backplane"]