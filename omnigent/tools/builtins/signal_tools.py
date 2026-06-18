"""Native signal tools over the durable signal bus (BDP-2248 α1 integration, ADR-0142).

The agent-facing half of the durable signal/await bus — the ``await_signal``
re-home's agent surface. ``signal_await`` registers a durable wait so an external
event (a TeamCity build callback, a webhook, a peer) can resume the work later;
``signal_deliver`` resolves a wait by id (idempotent on replay; unmatched ids
dead-letter); ``signal_check`` drains this session's delivered payloads. The
session is stamped **server-side** from ``ToolContext.conversation_id``.
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext


class SignalAwaitTool(Tool):
    """Register a durable wait for an external signal."""

    @classmethod
    def name(cls) -> str:
        return "signal_await"

    @classmethod
    def description(cls) -> str:
        return (
            "Register a durable wait for an external signal (a build callback, a "
            "webhook, a peer). Park the work now; resume when signal_check shows it "
            "arrived. Survives restarts."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "signal_await",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "signal_id": {
                            "type": "string",
                            "description": "Correlation id the deliverer uses, e.g. 'release:1.2.3'.",
                        },
                        "key": {
                            "type": "string",
                            "description": "A human label for the wait.",
                        },
                        "target": {
                            "type": "string",
                            "description": "The external source, e.g. 'teamcity'. Optional.",
                        },
                    },
                    "required": ["signal_id", "key"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        signal_id = args.get("signal_id")
        key = args.get("key")
        if not signal_id or not key:
            return json.dumps({"error": "missing required 'signal_id' or 'key'"})
        if not ctx.conversation_id:
            return json.dumps({"error": "signal_await requires a session"})

        from omnigent.runtime import get_signal_bus

        get_signal_bus().register_wait(
            signal_id=signal_id,
            session_id=ctx.conversation_id,
            key=key,
            target=args.get("target"),
        )
        return json.dumps({"signal_id": signal_id, "status": "pending"})


class SignalDeliverTool(Tool):
    """Deliver a signal by id, resolving a parked wait."""

    @classmethod
    def name(cls) -> str:
        return "signal_deliver"

    @classmethod
    def description(cls) -> str:
        return (
            "Deliver a signal by id, resolving a parked wait. Idempotent on replay "
            "(already_resolved); an unmatched id dead-letters instead of silently "
            "succeeding."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "signal_deliver",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "signal_id": {
                            "type": "string",
                            "description": "The waiting signal's correlation id.",
                        },
                        "payload": {
                            "type": "object",
                            "description": "Optional payload delivered to the waiter.",
                        },
                    },
                    "required": ["signal_id"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx
        args: dict[str, Any] = json.loads(arguments)
        signal_id = args.get("signal_id")
        if not signal_id:
            return json.dumps({"error": "missing required 'signal_id'"})

        from omnigent.runtime import get_signal_bus

        result = get_signal_bus().deliver(signal_id=signal_id, payload=args.get("payload"))
        return json.dumps({"signal_id": signal_id, "status": result.status.value})


class SignalCheckTool(Tool):
    """Drain this session's delivered signal payloads."""

    @classmethod
    def name(cls) -> str:
        return "signal_check"

    @classmethod
    def description(cls) -> str:
        return (
            "Check your session's inbox for delivered signal payloads (FIFO) and "
            "drain them. Call this to see whether an awaited signal has arrived."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "signal_check",
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments
        if not ctx.conversation_id:
            return json.dumps({"error": "signal_check requires a session"})

        from omnigent.runtime import get_signal_bus

        msgs = get_signal_bus().drain_inbox(session_id=ctx.conversation_id)
        signals = [
            {"signal_id": m.get("signal_id"), "payload": m.get("payload")} for m in msgs
        ]
        return json.dumps({"signals": signals})
