"""Native goals-backlog tools over the goal store (BDP-2271 C3 integration, ADR-0142).

The agent-facing why-act substrate: ``goal_create`` files a goal into the shared
backlog, ``goal_list`` reads it, ``goal_claim`` atomically takes ownership, and
``goal_advance`` moves a goal through its lifecycle. The owner on a claim is
stamped **server-side** from ``ToolContext.agent_id`` (anti-spoofing, ADR-0136).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_STATUSES = ("open", "assigned", "in_progress", "blocked", "done")


class GoalCreateTool(Tool):
    """File a goal into the shared backlog."""

    @classmethod
    def name(cls) -> str:
        return "goal_create"

    @classmethod
    def description(cls) -> str:
        return (
            "File a goal into the shared org backlog for an agent to pull and own. "
            "Lower priority numbers are pulled first."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_create",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "The goal."},
                        "priority": {
                            "type": "integer",
                            "description": "1 (urgent) .. 5 (someday); default 3.",
                            "default": 3,
                        },
                    },
                    "required": ["title"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        title = args.get("title")
        if not title:
            return json.dumps({"error": "missing required 'title'"})
        from bytedesk_omnigent.goals import get_goal_store

        goal = get_goal_store().create_goal(
            title=title, priority=int(args.get("priority", 3)), source=ctx.agent_id
        )
        return json.dumps({"goal_id": goal.id, "status": goal.status})


class GoalListTool(Tool):
    """List goals in the backlog (optionally by status)."""

    @classmethod
    def name(cls) -> str:
        return "goal_list"

    @classmethod
    def description(cls) -> str:
        return "List goals in the shared backlog, optionally filtered by status."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_list",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": list(_STATUSES),
                            "description": "Filter by status; omit for all.",
                        },
                        "mine": {
                            "type": "boolean",
                            "description": "Only goals you own (default false).",
                            "default": False,
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        from bytedesk_omnigent.goals import get_goal_store

        owner = ctx.agent_id if args.get("mine") else None
        goals = get_goal_store().list_goals(status=args.get("status"), owner_agent_id=owner)
        out = [
            {"goal_id": g.id, "title": g.title, "status": g.status, "priority": g.priority}
            for g in goals
        ]
        return json.dumps({"goals": out})


class GoalClaimTool(Tool):
    """Atomically claim an open goal."""

    @classmethod
    def name(cls) -> str:
        return "goal_claim"

    @classmethod
    def description(cls) -> str:
        return (
            "Take ownership of an open goal. Exactly one agent wins a claim; if it "
            "was already taken this returns claimed=false."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_claim",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "The goal to claim."}
                    },
                    "required": ["goal_id"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        goal_id = args.get("goal_id")
        if not goal_id:
            return json.dumps({"error": "missing required 'goal_id'"})
        if not ctx.agent_id:
            return json.dumps({"error": "goal_claim requires an agent identity"})
        from bytedesk_omnigent.goals import get_goal_store

        claimed = get_goal_store().claim_goal(goal_id=goal_id, owner_agent_id=ctx.agent_id)
        return json.dumps({"claimed": claimed})


class GoalAdvanceTool(Tool):
    """Move a goal to a new status."""

    @classmethod
    def name(cls) -> str:
        return "goal_advance"

    @classmethod
    def description(cls) -> str:
        return (
            "Move a goal you own to a new status (in_progress / blocked / done). A "
            "blocked goal is escalated by the accountability loop; a stalled one is "
            "reopened."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_advance",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "The goal."},
                        "status": {
                            "type": "string",
                            "enum": list(_STATUSES),
                            "description": "The new status.",
                        },
                    },
                    "required": ["goal_id", "status"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        goal_id = args.get("goal_id")
        status = args.get("status")
        if not goal_id or not status:
            return json.dumps({"error": "missing required 'goal_id' or 'status'"})
        if status not in _STATUSES:
            return json.dumps({"error": f"invalid status {status!r}; expected {list(_STATUSES)}"})
        # BDP-2285 — an agent may only advance a goal it OWNS; require identity and
        # scope the write so a foreign / non-existent goal is not reported as moved.
        if not ctx.agent_id:
            return json.dumps({"error": "goal_advance requires an agent identity"})
        from bytedesk_omnigent.goals import get_goal_store

        advanced = get_goal_store().advance_goal_owned(
            goal_id=goal_id, status=status, owner_agent_id=ctx.agent_id
        )
        if not advanced:
            return json.dumps(
                {"advanced": False, "goal_id": goal_id,
                 "error": "goal not found or not owned by you"}
            )
        return json.dumps({"advanced": True, "goal_id": goal_id, "status": status})
