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
_TARGET_KINDS = ("organization", "department", "agent")
_READINESS_KINDS = ("immediate", "dependent", "deferred")
_DEPENDENCY_KINDS = ("manual", "goal", "system_state")
_DEPENDENCY_STATUSES = ("pending", "satisfied", "waived")


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
                        "target_kind": {
                            "type": "string",
                            "enum": list(_TARGET_KINDS),
                            "description": (
                                "Who this goal is for: organization, department, or agent."
                            ),
                            "default": "organization",
                        },
                        "target_id": {
                            "type": "string",
                            "description": (
                                "Department or agent id. Optional for organization goals."
                            ),
                        },
                        "target_label": {
                            "type": "string",
                            "description": "Human-readable target label.",
                        },
                        "readiness_kind": {
                            "type": "string",
                            "enum": list(_READINESS_KINDS),
                            "description": "immediate, dependent, or deferred.",
                            "default": "immediate",
                        },
                        "dependencies": {
                            "type": "array",
                            "description": "Unblock conditions for dependent goals.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": list(_DEPENDENCY_KINDS),
                                        "default": "manual",
                                    },
                                    "ref": {"type": "string"},
                                    "label": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": list(_DEPENDENCY_STATUSES),
                                        "default": "pending",
                                    },
                                },
                                "required": ["label"],
                            },
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
            title=title,
            priority=int(args.get("priority", 3)),
            source=ctx.agent_id,
            target_kind=args.get("target_kind", "organization"),
            target_id=args.get("target_id"),
            target_label=args.get("target_label"),
            readiness_kind=args.get("readiness_kind", "immediate"),
            dependencies=args.get("dependencies") or None,
        )
        return json.dumps(
            {
                "goal_id": goal.id,
                "status": goal.status,
                "target_kind": goal.target_kind,
                "target_id": goal.target_id,
                "readiness_kind": goal.readiness_kind,
                "activation_state": goal.activation_state,
            }
        )


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
                        "target_kind": {
                            "type": "string",
                            "enum": list(_TARGET_KINDS),
                            "description": "Filter by target kind.",
                        },
                        "target_id": {
                            "type": "string",
                            "description": "Filter by target id.",
                        },
                        "readiness_kind": {
                            "type": "string",
                            "enum": list(_READINESS_KINDS),
                            "description": "Filter by readiness frame.",
                        },
                        "ready_only": {
                            "type": "boolean",
                            "description": "Only list claimable goals.",
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
        goals = get_goal_store().list_goals(
            status=args.get("status"),
            owner_agent_id=owner,
            target_kind=args.get("target_kind"),
            target_id=args.get("target_id"),
            readiness_kind=args.get("readiness_kind"),
            ready_only=bool(args.get("ready_only")),
        )
        out = [
            {
                "goal_id": g.id,
                "title": g.title,
                "status": g.status,
                "priority": g.priority,
                "target_kind": g.target_kind,
                "target_id": g.target_id,
                "readiness_kind": g.readiness_kind,
                "activation_state": g.activation_state,
            }
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


class GoalDependencyUpdateTool(Tool):
    """Resolve or revise a goal dependency."""

    @classmethod
    def name(cls) -> str:
        return "goal_dependency_update"

    @classmethod
    def description(cls) -> str:
        return "Update a goal dependency, typically marking it satisfied or waived."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_dependency_update",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string"},
                        "dependency_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": list(_DEPENDENCY_STATUSES),
                        },
                        "label": {"type": "string"},
                        "ref": {"type": "string"},
                    },
                    "required": ["goal_id", "dependency_id"],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        goal_id = args.get("goal_id")
        dependency_id = args.get("dependency_id")
        if not goal_id or not dependency_id:
            return json.dumps({"error": "missing required 'goal_id' or 'dependency_id'"})
        from bytedesk_omnigent.goals import get_goal_store

        updates = {k: args[k] for k in ("status", "label", "ref") if k in args}
        dependency = get_goal_store().update_dependency(
            goal_id=goal_id,
            dependency_id=dependency_id,
            **updates,
        )
        if dependency is None:
            return json.dumps({"updated": False, "error": "goal dependency not found"})
        return json.dumps(
            {
                "updated": True,
                "dependency_id": dependency.id,
                "status": dependency.status,
            }
        )
