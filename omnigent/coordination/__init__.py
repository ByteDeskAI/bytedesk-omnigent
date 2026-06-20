"""Pluggable cross-replica coordination for omnigent-server."""

from omnigent.coordination.factory import (
    get_coordination_registry,
    resolve_coordination_backplane,
)
from omnigent.coordination.protocol import (
    CoordinationBackplane,
    KV_PENDING,
    KV_PRESENCE,
    KV_REGISTRY,
    STREAM_COORD_EVENTS,
    ResourceKind,
)
from omnigent.coordination.replica_id import server_replica_id

__all__ = [
    "CoordinationBackplane",
    "KV_PENDING",
    "KV_PRESENCE",
    "KV_REGISTRY",

    "STREAM_COORD_EVENTS",
    "ResourceKind",
    "get_coordination_registry",
    "resolve_coordination_backplane",
    "server_replica_id",
]