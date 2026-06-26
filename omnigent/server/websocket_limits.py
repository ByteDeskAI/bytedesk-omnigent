"""Server WebSocket size limits."""

from __future__ import annotations

CONTROL_WEBSOCKET_MAX_MESSAGE_BYTES = 100 * 1024 * 1024

__all__ = ["CONTROL_WEBSOCKET_MAX_MESSAGE_BYTES"]
