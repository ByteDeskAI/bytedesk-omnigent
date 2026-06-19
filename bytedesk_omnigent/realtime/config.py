"""Realtime bridge configuration (BDP-2301).

The bridge publishes to the platform Redis that ByteDesk.Realtime fans out to
SignalR — the same in-cluster instance the Office service publishes
``office:presence``/``office:goals`` to. Tenant is env-configured (single-org
for now; the multi-tenant resolver is the known follow-up).
"""

from __future__ import annotations

import os

#: Platform Redis URL (same instance Office publishes presence/goals to).
REDIS_URL = os.getenv(
    "BYTEDESK_REALTIME_REDIS_URL", "redis://bytedesk-redis-master:6379"
)


def tenant_id() -> str | None:
    """The ByteDesk tenant whose org chart subscribes to ``office:agents``.

    The Redis channel embeds it as a DASHED guid (BDP-1397); it must equal the
    tenant the Realtime service resolves from the subscriber's JWT. Returns
    ``None`` when unset — the bridge then stays dormant (no hardcoded fallback).
    """
    value = os.getenv("BYTEDESK_REALTIME_TENANT_ID")
    return value or None
