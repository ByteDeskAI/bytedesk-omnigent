"""Native business-outcome tool over the outcome ledger (BDP-2268 B7 integration, ADR-0142).

``outcome_record`` lets an agent log an attributed business outcome (a won deal,
a resolved ticket, a shipped feature); the ledger rolls it into the agent's
cumulative scoreboard metric so find-specialist ranking learns what worked. The
attributed agent is stamped **server-side** from ``ToolContext.agent_id``
(anti-spoofing, ADR-0136) — you record your own results.
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext


class OutcomeRecordTool(Tool):
    """Log an attributed business outcome."""

    @classmethod
    def name(cls) -> str:
        return "outcome_record"

    @classmethod
    def description(cls) -> str:
        return (
            "Record a business outcome you achieved (a won deal, a resolved ticket, "
            "a shipped feature) under a metric. It rolls into your scoreboard so the "
            "org learns who is good at what."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "outcome_record",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "Outcome kind, e.g. 'deal_won', 'ticket_resolved'.",
                        },
                        "metric": {
                            "type": "string",
                            "description": "Scoreboard metric to roll into (e.g. 'revenue').",
                        },
                        "value": {
                            "type": "number",
                            "description": "Magnitude (deal size, count). Default 1.",
                            "default": 1,
                        },
                        "ref": {
                            "type": "string",
                            "description": "Optional external ref (deal id, ticket key).",
                        },
                    },
                    "required": ["kind", "metric"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        kind = args.get("kind")
        metric = args.get("metric")
        if not kind or not metric:
            return json.dumps({"error": "missing required 'kind' or 'metric'"})
        if not ctx.agent_id:
            return json.dumps({"error": "outcome_record requires an agent identity"})
        from omnigent.outcomes import get_outcome_ledger

        outcome = get_outcome_ledger().record_outcome(
            agent_id=ctx.agent_id,
            kind=kind,
            metric=metric,
            value=float(args.get("value", 1)),
            ref=args.get("ref"),
        )
        return json.dumps({"outcome_id": outcome.id, "metric": outcome.metric})
