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

        from omnigent.goals import get_goal_store

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
