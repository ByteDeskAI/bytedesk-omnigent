"""Native goals-backlog tools over the goal store (BDP-2271 C3 integration, ADR-0142).

The agent-facing why-act substrate: ``goal_create`` files a goal into the shared
backlog, ``goal_list`` reads it, ``goal_claim`` atomically takes ownership, and
``goal_advance`` moves a goal through its lifecycle. The owner on a claim is
stamped **server-side** from ``ToolContext.agent_id`` (anti-spoofing, ADR-0136).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_STATUSES = ("open", "assigned", "in_progress", "blocked", "done")
_TARGET_KINDS = ("organization", "department", "agent")
_READINESS_KINDS = ("immediate", "dependent", "deferred")
_DEPENDENCY_KINDS = ("manual", "goal", "system_state")
_DEPENDENCY_STATUSES = ("pending", "satisfied", "waived")
_POSTURES = ("gated", "full_auto")

# Wave-6 founder-org arm switch (BDP-2599): until this is set, the command-center
# commander tool can DISARM (set 'gated', the kill switch) but cannot ARM full_auto.
_ARMING_ENV = "BYTEDESK_GOALS_ARMING_ENABLED"


def _arming_enabled() -> bool:
    return os.getenv(_ARMING_ENV, "").strip().lower() in ("1", "true", "yes", "on")


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
                        "proposed": {
                            "type": "boolean",
                            "description": (
                                "File this as a governance DRAFT (a proposal) instead of "
                                "an active goal — it is parked for human/manager approval "
                                "and is NOT auto-dispatched. Use this when proposing "
                                "discovered opportunities (the scout path)."
                            ),
                            "default": False,
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why this goal is worth doing (shown at approval).",
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

        store = get_goal_store()
        if args.get("proposed"):
            # Governance draft path (BDP-2596 scout): never auto-armed.
            from bytedesk_omnigent.engine.scout import propose_goal

            goal = propose_goal(
                store,
                title=title,
                source=ctx.agent_id or "scout",
                rationale=args.get("rationale"),
                target_kind=args.get("target_kind", "organization"),
                target_id=args.get("target_id"),
                target_label=args.get("target_label"),
            )
        else:
            goal = store.create_goal(
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


# ── Command-center commander tools (BDP-2598) ─────────────────────────────────
# The full engine-driving toolset for the goal-commander agent: reorder/repriority,
# treasury caps, the arm switch, the read projections (frontier/decisions/ledger),
# batch-activate, and decompose. Reads go through engine/treasury/optimizer; writes
# mirror the existing tool/store pattern. full_auto arming is governance-gated.


class GoalPrioritizeTool(Tool):
    """Reorder / repriority a set of goals (assign priority by position)."""

    @classmethod
    def name(cls) -> str:
        return "goal_prioritize"

    @classmethod
    def description(cls) -> str:
        return (
            "Repriority goals. Pass goal_ids with an explicit 'order' (priority 1.."
            "N by position, lower=more urgent), or pass a 'priority' map "
            "{goal_id: priority}. Lower priority numbers are pulled first."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_prioritize",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Goals in desired priority order (first = priority 1).",
                        },
                        "order": {
                            "type": "boolean",
                            "description": "Assign priority 1..N from goal_ids position.",
                            "default": True,
                        },
                        "priority": {
                            "type": "object",
                            "description": "Explicit {goal_id: priority} map (overrides order).",
                            "additionalProperties": {"type": "integer"},
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        explicit = args.get("priority")
        goal_ids = args.get("goal_ids") or []
        if explicit:
            assignments = {str(gid): int(p) for gid, p in explicit.items()}
        elif goal_ids:
            assignments = {str(gid): i + 1 for i, gid in enumerate(goal_ids)}
        else:
            return json.dumps({"error": "provide 'goal_ids' or a 'priority' map"})
        from bytedesk_omnigent.goals import get_goal_store

        store = get_goal_store()
        updated: list[dict[str, Any]] = []
        for goal_id, priority in assignments.items():
            goal = store.update_goal(goal_id=goal_id, priority=priority)
            if goal is not None:
                updated.append({"goal_id": goal.id, "priority": goal.priority})
        return json.dumps({"updated": updated})


class GoalAdjustBudgetTool(Tool):
    """Patch the treasury budget for a goal's scope (governance)."""

    @classmethod
    def name(cls) -> str:
        return "goal_adjust_budget"

    @classmethod
    def description(cls) -> str:
        return (
            "Set the treasury cap/limits for a scope. Pass goal_id (resolves the "
            "goal's tier+target scope) OR an explicit tier+target_id. Omitted "
            "limits are cleared to None except cap_cents (defaults 0=uncapped)."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_adjust_budget",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "Resolve scope from a goal."},
                        "tier": {"type": "string", "description": "Scope tier (with target_id)."},
                        "target_id": {"type": "string", "description": "Scope target id."},
                        "cap_cents": {"type": "integer", "description": "Spend cap (cents)."},
                        "cap_tokens": {"type": "integer", "description": "Token cap."},
                        "max_spawns": {"type": "integer", "description": "Max concurrent spawns."},
                        "anomaly_threshold_cents": {
                            "type": "integer",
                            "description": "Circuit-breaker anomaly threshold (cents).",
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        from bytedesk_omnigent.engine.treasury import get_treasury
        from bytedesk_omnigent.goals import get_goal_store

        tier = args.get("tier")
        target_id = args.get("target_id")
        goal_id = args.get("goal_id")
        if goal_id:
            goal = get_goal_store().get_goal(goal_id=goal_id, include_dependencies=False)
            if goal is None:
                return json.dumps({"error": "goal not found"})
            tier, target_id = goal.tier, goal.target_id
        if not tier or not target_id:
            return json.dumps({"error": "provide 'goal_id' or both 'tier' and 'target_id'"})
        treasury = get_treasury()
        treasury.set_budget(
            tier=tier,
            target_id=target_id,
            cap_cents=int(args["cap_cents"]) if args.get("cap_cents") is not None else 0,
            cap_tokens=args.get("cap_tokens"),
            max_spawns=args.get("max_spawns"),
            anomaly_threshold_cents=args.get("anomaly_threshold_cents"),
        )
        return json.dumps(
            {
                "scope": f"{tier}:{target_id}",
                "spent_cents": treasury.spent_cents(tier=tier, target_id=target_id),
            }
        )


class GoalSetPostureTool(Tool):
    """Arm/disarm the autonomy posture (the command-center switch, governance)."""

    @classmethod
    def name(cls) -> str:
        return "goal_set_posture"

    @classmethod
    def description(cls) -> str:
        return (
            "Set the goal-engine autonomy posture. 'gated' (the kill switch) is "
            "always allowed; arming 'full_auto' is governance-gated and refused "
            "until the founder-org arm switch is enabled. Pass target_id to scope "
            "the posture to one tenant, omit for the global default."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_set_posture",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "posture": {
                            "type": "string",
                            "enum": list(_POSTURES),
                            "description": "gated (governed) or full_auto (armed).",
                        },
                        "target_id": {
                            "type": "string",
                            "description": "Tenant id to scope the posture to; omit for global.",
                        },
                    },
                    "required": ["posture"],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        posture = args.get("posture")
        if posture not in _POSTURES:
            return json.dumps({"error": f"invalid posture; expected {list(_POSTURES)}"})
        # Governance gate: only DISARMING is always allowed; arming full_auto is the
        # high-blast-radius switch and stays refused until Wave-6 founder-org arming.
        if posture == "full_auto" and not _arming_enabled():
            return json.dumps(
                {
                    "error": "full_auto arming is governance-gated",
                    "armed": False,
                    "hint": f"set {_ARMING_ENV}=1 (founder-org arming, BDP-2599) to enable",
                }
            )
        from bytedesk_omnigent.engine.config import set_autonomy_posture

        target_id = args.get("target_id")
        written = asyncio.run(set_autonomy_posture(posture, tenant_id=target_id))
        return json.dumps(
            {"posture": written, "target_id": target_id, "armed": written == "full_auto"}
        )


class GoalReadFrontierTool(Tool):
    """Read the actionable+ranked frontier (ROI + waiting_reasons)."""

    @classmethod
    def name(cls) -> str:
        return "goal_read_frontier"

    @classmethod
    def description(cls) -> str:
        return (
            "Read the ROI frontier: the ready, owned goals the engine would fund, "
            "ranked by risk-decayed ROI, each with roi, actionable, and "
            "waiting_reasons. Optionally filter by target_kind/target_id."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_read_frontier",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_kind": {"type": "string", "enum": list(_TARGET_KINDS)},
                        "target_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        from bytedesk_omnigent.engine.frontier import build_frontier
        from bytedesk_omnigent.engine.sensors import build_default_registry
        from bytedesk_omnigent.engine.treasury import get_treasury
        from bytedesk_omnigent.goals import get_goal_store

        rows = build_frontier(
            goal_store=get_goal_store(),
            sensor_registry=build_default_registry(),
            treasury=get_treasury(),
            target_kind=args.get("target_kind"),
            target_id=args.get("target_id"),
        )
        return json.dumps({"frontier": rows})


class GoalReadDecisionsTool(Tool):
    """Read the fund/skip decision-replay log."""

    @classmethod
    def name(cls) -> str:
        return "goal_read_decisions"

    @classmethod
    def description(cls) -> str:
        return "Read the engine's fund/skip decision replay log, newest tick last."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_read_decisions",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "Filter to one goal."},
                        "limit": {"type": "integer", "description": "Cap results (most recent)."},
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        from dataclasses import asdict

        from bytedesk_omnigent.engine.treasury import get_treasury

        decisions = get_treasury().decisions(goal_id=args.get("goal_id"))
        limit = args.get("limit")
        if isinstance(limit, int) and limit > 0:
            decisions = decisions[-limit:]
        return json.dumps({"decisions": [asdict(d) for d in decisions]})


class GoalReadLedgerTool(Tool):
    """Read the realized-value (outcomes) ledger."""

    @classmethod
    def name(cls) -> str:
        return "goal_read_ledger"

    @classmethod
    def description(cls) -> str:
        return (
            "Read the realized-value ledger (booked outcomes). Filter by goal_id, or "
            "by target_kind/target_id to roll up a scope's booked value."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_read_ledger",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string"},
                        "target_kind": {"type": "string", "enum": list(_TARGET_KINDS)},
                        "target_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
        from dataclasses import asdict

        from bytedesk_omnigent.engine.treasury import get_treasury
        from bytedesk_omnigent.goals import get_goal_store

        treasury = get_treasury()
        outcomes = treasury.outcomes(goal_id=args.get("goal_id"))
        # Scope filter: keep outcomes whose goal matches the target (best-effort read).
        target_kind = args.get("target_kind")
        target_id = args.get("target_id")
        if (target_kind or target_id) and args.get("goal_id") is None:
            store = get_goal_store()
            kept = []
            for o in outcomes:
                g = store.get_goal(goal_id=o.goal_id, include_dependencies=False)
                if g is None:
                    continue
                if target_kind and g.target_kind != target_kind:
                    continue
                if target_id and g.target_id != target_id:
                    continue
                kept.append(o)
            outcomes = kept
        total = sum(o.realized_value_cents for o in outcomes)
        return json.dumps(
            {"outcomes": [asdict(o) for o in outcomes], "realized_value_cents": total}
        )


class GoalBatchApproveTool(Tool):
    """Multi-activate proposed/draft goals (governance)."""

    @classmethod
    def name(cls) -> str:
        return "goal_batch_approve"

    @classmethod
    def description(cls) -> str:
        return (
            "Approve and activate a set of proposed/draft goals so they become "
            "claimable and enter dispatch. Returns per-goal approved/not-found."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_batch_approve",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Proposed/draft goals to activate.",
                        }
                    },
                    "required": ["goal_ids"],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        goal_ids = args.get("goal_ids") or []
        if not goal_ids:
            return json.dumps({"error": "missing required 'goal_ids'"})
        from bytedesk_omnigent.goals import get_goal_store

        store = get_goal_store()
        results: list[dict[str, Any]] = []
        for goal_id in goal_ids:
            goal = store.activate_goal(goal_id=goal_id)
            results.append(
                {"goal_id": goal_id, "approved": goal is not None,
                 "activation_state": goal.activation_state if goal else None}
            )
        return json.dumps({"results": results})


class GoalDecomposeTool(Tool):
    """Split a parent goal into a child-goal tree (engine decompose)."""

    @classmethod
    def name(cls) -> str:
        return "goal_decompose"

    @classmethod
    def description(cls) -> str:
        return (
            "Break a parent goal into child goals (each needs a 'title'; other "
            "fields inherit from the parent unless overridden). Children land under "
            "the parent's budget scope and roll their realized value up to it."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "goal_decompose",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "The parent goal."},
                        "spec": {
                            "type": "array",
                            "description": "Child goals; each entry needs a 'title'.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "priority": {"type": "integer"},
                                    "readiness_kind": {
                                        "type": "string", "enum": list(_READINESS_KINDS)
                                    },
                                    "risk_tier": {"type": "string"},
                                    "expected_value_cents": {"type": "integer"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["title"],
                            },
                        },
                    },
                    "required": ["goal_id", "spec"],
                },
            },
        }

    def invoke(self, arguments: str, _ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        goal_id = args.get("goal_id")
        spec = args.get("spec")
        if not goal_id or spec is None:
            return json.dumps({"error": "missing required 'goal_id' or 'spec'"})
        from bytedesk_omnigent.engine.decompose import decompose_goal
        from bytedesk_omnigent.goals import get_goal_store

        try:
            children = decompose_goal(get_goal_store(), parent_goal_id=goal_id, spec=spec)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "parent_goal_id": goal_id,
                "children": [{"goal_id": c.id, "title": c.title} for c in children],
            }
        )
