"""Sync helpers for registry → backplane claims."""

from __future__ import annotations

from omnigent.coordination.lifecycle import schedule_backplane
from omnigent.coordination.protocol import ResourceKind


def claim_resource(kind: ResourceKind, resource_id: str) -> None:
    """Record a live tunnel resource on the active backplane (best-effort)."""
    from omnigent.coordination.lifecycle import get_active_backplane

    backplane = get_active_backplane()
    if backplane is None:
        return
    schedule_backplane(backplane.claim_resource(kind, resource_id))


def release_resource(kind: ResourceKind, resource_id: str) -> None:
    """Drop a tunnel resource claim on the active backplane (best-effort)."""
    from omnigent.coordination.lifecycle import get_active_backplane

    backplane = get_active_backplane()
    if backplane is None:
        return
    schedule_backplane(backplane.release_resource(kind, resource_id))


__all__ = ["claim_resource", "release_resource"]