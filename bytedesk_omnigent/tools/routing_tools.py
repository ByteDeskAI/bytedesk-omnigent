"""Native self-learning routing tool over the outcome scoreboard (BDP-2276 E2, ADR-0142).

``find_specialist`` ranks agents for a metric by what actually worked — it reads
the cumulative ``scoreboard_entries`` the Business Outcome Ledger upserts
(``omnigent/outcomes.py`` → ``omnigent/goals.py`` scoreboard). Because
``outcome_record`` rolls each recorded result into that scoreboard, routing by
this tool *learns*: the more an agent delivers on a metric, the higher it ranks.
This is the omnigent-native equivalent of ADR-0103's ``performance.jsonl`` →
FindSpecialistService loop.

Read-only — ranking is public org information, so no agent identity is required.
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 50


def _persisted_capabilities(agent_id: str) -> tuple[str, ...]:
    """Read an agent's persisted capability slugs (BDP-2334), or empty.

    Degrades to an empty tuple on any store error — a read-only ranking tool
    must never crash because the roster could not be enriched.
    """
    try:
        from omnigent.runtime import get_agent_store

        return get_agent_store().get_capabilities(agent_id)
    except Exception:  # noqa: BLE001 - enrichment is best-effort
        return ()


class FindSpecialistTool(Tool):
    """Rank agents for a metric by their cumulative recorded outcomes."""

    @classmethod
    def name(cls) -> str:
        return "find_specialist"

    @classmethod
    def description(cls) -> str:
        return (
            "Find the agents who are demonstrably best at a metric, ranked by their "
            "cumulative recorded outcomes (the self-learning scoreboard). Use before "
            "delegating work so it routes to whoever has actually delivered on that "
            "metric, not who is merely nominally responsible."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "find_specialist",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "description": (
                                "Scoreboard metric to rank by, e.g. 'revenue', "
                                "'tickets', 'ships'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Max candidates (default {_DEFAULT_LIMIT}).",
                            "default": _DEFAULT_LIMIT,
                        },
                    },
                    "required": ["metric"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # read-only ranking; no agent identity required.
        args: dict[str, Any] = json.loads(arguments)
        metric = args.get("metric")
        if not metric:
            return json.dumps({"error": "missing required 'metric' argument"})
        try:
            limit = max(1, min(int(args.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT

        from bytedesk_omnigent.goals import get_goal_store

        board = get_goal_store().scoreboard(metric=metric, limit=limit)
        candidates = [{"agent_id": agent_id, "score": value} for agent_id, value in board]
        if not candidates:
            return json.dumps(
                {
                    "metric": metric,
                    "candidates": [],
                    "message": "No recorded outcomes for this metric yet.",
                }
            )
        return json.dumps({"metric": metric, "candidates": candidates})


class ResolveAssigneeTool(Tool):
    """Resolve who should own a piece of work via the assignment chain."""

    @classmethod
    def name(cls) -> str:
        return "resolve_assignee"

    @classmethod
    def description(cls) -> str:
        return (
            "Resolve the right owner for a task: an explicit owner wins; otherwise "
            "narrow the roster to agents who hold the required capability AND sit in "
            "the required department, then rank those survivors by the self-learning "
            "scoreboard for the metric. Filter on capability first, rank second."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "resolve_assignee",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "description": "Scoreboard metric to rank eligible agents by.",
                        },
                        "roster": {
                            "type": "array",
                            "description": (
                                "Candidate agents: each {agent_id, department?, "
                                "capabilities: [slug, ...]}."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "agent_id": {"type": "string"},
                                    "department": {"type": "string"},
                                    "capabilities": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["agent_id"],
                            },
                        },
                        "explicit_owner": {
                            "type": "string",
                            "description": "Pre-chosen owner; short-circuits filter + rank.",
                        },
                        "capability": {
                            "type": "string",
                            "description": "Required capability slug (omit = no filter).",
                        },
                        "department": {
                            "type": "string",
                            "description": "Required department (omit = no filter).",
                        },
                    },
                    "required": ["metric", "roster"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # read-only resolution; no agent identity required.
        from bytedesk_omnigent.assignment import CandidateAgent, resolve_assignee

        args: dict[str, Any] = json.loads(arguments)
        metric = args.get("metric")
        if not metric:
            return json.dumps({"error": "missing required 'metric' argument"})
        roster = [
            CandidateAgent(
                agent_id=str(e["agent_id"]),
                department=e.get("department"),
                # Arg-supplied capabilities win; otherwise fall back to the
                # persisted capability surface (BDP-2334) so the resolver reads
                # real declared capabilities even when the caller omits them.
                capabilities=tuple(e["capabilities"])
                if e.get("capabilities") is not None
                else _persisted_capabilities(str(e["agent_id"])),
            )
            for e in args.get("roster", [])
            if e.get("agent_id")
        ]
        result = resolve_assignee(
            metric=metric,
            roster=roster,
            explicit_owner=args.get("explicit_owner"),
            capability=args.get("capability"),
            department=args.get("department"),
        )
        return json.dumps(
            {
                "metric": metric,
                "assignee": result.assignee,
                "reason": result.reason,
                "ranked": list(result.ranked),
            }
        )
