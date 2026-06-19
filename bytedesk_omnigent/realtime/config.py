"""Realtime bridge configuration (BDP-2301).

Values are sourced from Infisical — project **"ByteDesk Agent Configuration"** —
via the omnigent Infisical bootstrap, which merges secrets into ``os.environ`` at
startup (mirrors .NET ServiceDefaults). Everything here is read LAZILY (at emit
time, in the lifespan) so the bootstrap has already run before the first read;
nothing is captured at import. Local dev without Infisical falls back to the
plain env / the in-cluster default.

Required key (in "ByteDesk Agent Configuration"):
  BYTEDESK_TENANT_ID            — the ByteDesk org tenant GUID whose org chart
                                  subscribes to office:agents. (Realtime-specific
                                  override: BYTEDESK_REALTIME_TENANT_ID.)
Optional:
  BYTEDESK_REDIS_URL            — platform Redis ByteDesk.Realtime fans out from.
                                  Defaults to the in-cluster service. (Override:
                                  BYTEDESK_REALTIME_REDIS_URL.)
"""

from __future__ import annotations

import os

_DEFAULT_REDIS_URL = "redis://bytedesk-redis-master:6379"


def redis_url() -> str:
    """Platform Redis URL (same instance Office publishes presence/goals to)."""
    return (
        os.getenv("BYTEDESK_REALTIME_REDIS_URL")
        or os.getenv("BYTEDESK_REDIS_URL")
        or _DEFAULT_REDIS_URL
    )


def tenant_id() -> str | None:
    """The ByteDesk tenant whose org chart subscribes to ``office:agents``.

    The Redis channel embeds it as a DASHED guid (BDP-1397); it must equal the
    tenant the Realtime service resolves from the subscriber's JWT. Returns
    ``None`` when unset — the bridge then stays dormant (no hardcoded fallback).
    Prefers the realtime-specific override, then the canonical org tenant key.
    """
    value = os.getenv("BYTEDESK_REALTIME_TENANT_ID") or os.getenv("BYTEDESK_TENANT_ID")
    return value or None
