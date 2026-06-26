"""Shared WebSocket keepalive frames for host control connections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum


class KeepaliveFrameKind(str, Enum):
    """Keepalive frame kinds used by long-lived control WebSockets."""

    PING = "ping"
    PONG = "pong"


@dataclass(frozen=True)
class PingFrame:
    ts: int


@dataclass(frozen=True)
class PongFrame:
    ts: int


KeepaliveFrame = PingFrame | PongFrame


def encode_keepalive_frame(frame: KeepaliveFrame) -> str:
    if isinstance(frame, PingFrame):
        return json.dumps({"kind": KeepaliveFrameKind.PING.value, "ts": frame.ts})
    if isinstance(frame, PongFrame):
        return json.dumps({"kind": KeepaliveFrameKind.PONG.value, "ts": frame.ts})
    raise TypeError(f"unknown keepalive frame type: {type(frame).__name__}")


def decode_keepalive_frame(text: str) -> KeepaliveFrame:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ValueError(f"frame must be a JSON object, got {type(msg).__name__}")
    kind = msg.get("kind")
    if not isinstance(kind, str):
        raise ValueError("frame missing 'kind' field")
    ts = msg.get("ts")
    if not isinstance(ts, int):
        raise ValueError("keepalive frame missing integer 'ts'")
    try:
        frame_kind = KeepaliveFrameKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown keepalive frame kind: {kind!r}") from exc
    match frame_kind:
        case KeepaliveFrameKind.PING:
            return PingFrame(ts=ts)
        case KeepaliveFrameKind.PONG:
            return PongFrame(ts=ts)
    raise ValueError(f"unhandled keepalive frame kind: {kind!r}")  # pragma: no cover


__all__ = [
    "KeepaliveFrame",
    "KeepaliveFrameKind",
    "PingFrame",
    "PongFrame",
    "decode_keepalive_frame",
    "encode_keepalive_frame",
]
