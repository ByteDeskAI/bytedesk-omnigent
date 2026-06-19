"""Native peer-message tools over the peer bus (BDP-2270 C2 integration, ADR-0142).

The agent-facing half of the lateral social fabric — the ``sys_peer_message``
tool the C2 store shipped without. ``peer_send`` posts a DM / broadcast /
escalation; ``peer_inbox`` drains the caller's unread messages. The sender
(``from_agent``) / reader (``to_agent``) is stamped **server-side** from
``ToolContext.agent_id`` — an agent cannot spoof another's identity (the
ADR-0133/0136 anti-spoofing invariant, same as the memory tools).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_KINDS = ("dm", "broadcast", "escalation")


class PeerSendTool(Tool):
    """Send a lateral message to a peer (DM / broadcast / escalation)."""

    @classmethod
    def name(cls) -> str:
        return "peer_send"

    @classmethod
    def description(cls) -> str:
        return (
            "Message a peer agent sideways — ask, hand off, or escalate. Use a DM "
            "to one agent, a broadcast to a topic, or an escalation to raise a "
            "blocker. Not limited to your own sub-agents."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "peer_send",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic/thread key, e.g. 'pricing' or 'incident:42'.",
                        },
                        "body": {"type": "string", "description": "The message text."},
                        "to_agent": {
                            "type": "string",
                            "description": "Recipient agent id (DM); omit to broadcast.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": list(_KINDS),
                            "description": "dm (default) / broadcast / escalation.",
                            "default": "dm",
                        },
                    },
                    "required": ["topic", "body"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        topic = args.get("topic")
        body = args.get("body")
        if not topic or not body:
            return json.dumps({"error": "missing required 'topic' or 'body'"})
        if not ctx.agent_id:
            return json.dumps({"error": "peer_send requires an agent identity"})
        kind = args.get("kind", "dm")
        if kind not in _KINDS:
            return json.dumps({"error": f"invalid kind {kind!r}; expected {list(_KINDS)}"})

        from bytedesk_omnigent.peer import get_peer_message_store

        msg = get_peer_message_store().send(
            from_agent=ctx.agent_id,
            topic=topic,
            body=body,
            to_agent=args.get("to_agent"),
            kind=kind,
        )
        return json.dumps({"message_id": msg.id, "seq": msg.seq, "topic": msg.topic})


class PeerInboxTool(Tool):
    """Drain the caller's peer-message inbox."""

    @classmethod
    def name(cls) -> str:
        return "peer_inbox"

    @classmethod
    def description(cls) -> str:
        return (
            "Read messages other agents sent you. Defaults to unread only and "
            "marks them read. Check this to coordinate before acting."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "peer_inbox",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "unread_only": {
                            "type": "boolean",
                            "description": "Only unread messages (default true).",
                            "default": True,
                        },
                        "mark_read": {
                            "type": "boolean",
                            "description": "Mark the returned messages read (default true).",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        if not ctx.agent_id:
            return json.dumps({"error": "peer_inbox requires an agent identity"})

        from bytedesk_omnigent.peer import get_peer_message_store

        msgs = get_peer_message_store().inbox(
            to_agent=ctx.agent_id,
            unread_only=bool(args.get("unread_only", True)),
            mark_read=bool(args.get("mark_read", True)),
        )
        out = [
            {"from": m.from_agent, "topic": m.topic, "kind": m.kind, "body": m.body}
            for m in msgs
        ]
        return json.dumps({"messages": out})
