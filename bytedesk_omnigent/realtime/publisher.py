"""Best-effort sync Redis publisher to the platform Redis (BDP-2301).

AgentStore mutations are synchronous (the routes call them via
``asyncio.to_thread``), so the emit is a synchronous, fire-and-forget
``PUBLISH`` that never raises into the caller — a realtime hiccup must never
fail an agent write. The client is created lazily so importing the bridge has
no side effects and environments without the platform Redis simply never
connect.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from bytedesk_omnigent.realtime import config

if TYPE_CHECKING:
    import redis as redis_pkg

logger = logging.getLogger(__name__)

_client: redis_pkg.Redis | None = None


def _get_client() -> redis_pkg.Redis:
    global _client
    if _client is None:
        import redis  # lazy import — only when the bridge actually publishes

        _client = redis.Redis.from_url(
            config.REDIS_URL,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _client


def publish(channel: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget JSON publish; swallows + logs any failure (best-effort)."""
    try:
        _get_client().publish(channel, json.dumps(payload))
    except Exception as exc:  # noqa: BLE001 — realtime fan-out is best-effort
        logger.warning("office:agents publish to %s failed: %s", channel, exc)


def reset_client_for_test() -> None:
    """Drop the cached client so a test can swap REDIS_URL / inject a fake."""
    global _client
    _client = None
