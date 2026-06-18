"""Native deliberation tools over the deliberation store (BDP-2273 C6 integration, ADR-0142).

The agent-facing decision organ: ``deliberation_start`` opens a proposal,
``deliberation_position`` adds a for/against/amend argument, ``deliberation_decide``
records the outcome, and ``deliberation_find`` answers "what did we decide about
X?". The contributing / deciding agent is stamped **server-side** from
``ToolContext.agent_id`` (anti-spoofing, ADR-0136).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_STANCES = ("for", "against", "amend")


class DeliberationStartTool(Tool):
    """Open a proposal→debate→decision."""

    @classmethod
    def name(cls) -> str:
        return "deliberation_start"

    @classmethod
    def description(cls) -> str:
        return (
            "Open a deliberation on a topic with a proposal so the team decides by "
            "debate, not one prompt. Peers add positions; close it with a decision."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "deliberation_start",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "The subject."},
                        "proposal": {"type": "string", "description": "The opening proposal."},
                    },
                    "required": ["topic", "proposal"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        topic = args.get("topic")
        proposal = args.get("proposal")
        if not topic or not proposal:
            return json.dumps({"error": "missing required 'topic' or 'proposal'"})
        from omnigent.deliberation import get_deliberation_store

        delib = get_deliberation_store().start(
            topic=topic, proposal=proposal, opened_by=ctx.agent_id
        )
        return json.dumps({"deliberation_id": delib.id, "status": delib.status})


class DeliberationPositionTool(Tool):
    """Add a position to a deliberation round."""

    @classmethod
    def name(cls) -> str:
        return "deliberation_position"

    @classmethod
    def description(cls) -> str:
        return "Argue for, against, or amend an open deliberation's proposal."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "deliberation_position",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "deliberation_id": {"type": "string", "description": "The deliberation."},
                        "stance": {
                            "type": "string",
                            "enum": list(_STANCES),
                            "description": "for / against / amend.",
                        },
                        "body": {"type": "string", "description": "Your argument."},
                        "round": {
                            "type": "integer",
                            "description": "Debate round (default 1).",
                            "default": 1,
                        },
                    },
                    "required": ["deliberation_id", "stance", "body"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        delib_id = args.get("deliberation_id")
        stance = args.get("stance")
        body = args.get("body")
        if not delib_id or not stance or not body:
            return json.dumps({"error": "missing required 'deliberation_id'/'stance'/'body'"})
        if stance not in _STANCES:
            return json.dumps({"error": f"invalid stance {stance!r}; expected {list(_STANCES)}"})
        if not ctx.agent_id:
            return json.dumps({"error": "deliberation_position requires an agent identity"})
        from omnigent.deliberation import get_deliberation_store

        pos = get_deliberation_store().add_position(
            deliberation_id=delib_id,
            agent_id=ctx.agent_id,
            stance=stance,
            body=body,
            round=int(args.get("round", 1)),
        )
        return json.dumps({"position_id": pos.id, "round": pos.round})


class DeliberationDecideTool(Tool):
    """Close a deliberation with a recorded decision."""

    @classmethod
    def name(cls) -> str:
        return "deliberation_decide"

    @classmethod
    def description(cls) -> str:
        return (
            "Close an open deliberation by recording the decision. Only the first "
            "decide wins; returns decided=false if it was already closed."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "deliberation_decide",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "deliberation_id": {"type": "string", "description": "The deliberation."},
                        "decision": {"type": "string", "description": "The decided outcome."},
                    },
                    "required": ["deliberation_id", "decision"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        delib_id = args.get("deliberation_id")
        decision = args.get("decision")
        if not delib_id or not decision:
            return json.dumps({"error": "missing required 'deliberation_id' or 'decision'"})
        if not ctx.agent_id:
            return json.dumps({"error": "deliberation_decide requires an agent identity"})
        from omnigent.deliberation import get_deliberation_store

        decided = get_deliberation_store().decide(
            deliberation_id=delib_id, decision=decision, decided_by=ctx.agent_id
        )
        return json.dumps({"decided": decided})


class DeliberationFindTool(Tool):
    """Answer 'what did we decide about X?'."""

    @classmethod
    def name(cls) -> str:
        return "deliberation_find"

    @classmethod
    def description(cls) -> str:
        return "Recall the latest decision on a topic — 'what did we decide about X?'."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "deliberation_find",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "The subject to recall."}
                    },
                    "required": ["topic"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx
        args: dict[str, Any] = json.loads(arguments)
        topic = args.get("topic")
        if not topic:
            return json.dumps({"error": "missing required 'topic'"})
        from omnigent.deliberation import get_deliberation_store

        found = get_deliberation_store().find_decision(topic=topic)
        if found is None:
            return json.dumps(
                {"decision": None, "message": f"No decision recorded for {topic!r}."}
            )
        return json.dumps(
            {
                "deliberation_id": found.id,
                "decision": found.decision,
                "decided_by": found.decided_by,
            }
        )
