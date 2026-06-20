"""Built-in tools for the omnigent-native agent memory plane (FU1, ADR-0132).

Three tools over :class:`~omnigent.stores.memory_store.SqlAlchemyMemoryStore`:

- ``memory_append`` — save a durable, weighted memory into a compartment.
- ``memory_query`` — recall memories ranked by decayed salience.
- ``memory_compartments_list`` — list the caller's reachable compartments.

The compartment ``owner`` is stamped **server-side** from
``ToolContext.agent_id``; the LLM never supplies an owner. So an agent cannot
read or write another agent's private (``agent``-scope) memory — the
anti-spoofing invariant from ADR-0133/0136. ``team`` / ``topic`` compartments
are intentionally shared spaces keyed by the agent-chosen ``name``. The
``tenant`` scope is not exposed (omnigent has no tenant identity yet).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.memory_protocol import (
    ORG_CONTEXT_COMPARTMENT,
    ensure_org_compartments,
)
from omnigent.tools.base import Tool, ToolContext

_SCOPES = ("agent", "team", "topic")
_TEAM_OWNER = "team"
_TOPIC_OWNER = "shared"


def _resolve_owner(scope: str, ctx: ToolContext) -> str:
    """Return the server-derived compartment owner for *scope*.

    Never agent-supplied — that is the anti-spoofing guarantee.

    :param scope: One of ``agent`` / ``team`` / ``topic``.
    :param ctx: The tool execution context (carries the server-stamped
        ``agent_id``).
    :returns: The owner key.
    :raises ValueError: For an unknown scope, or an ``agent`` scope with no
        agent identity.
    """
    if scope == "agent":
        if not ctx.agent_id:
            raise ValueError("agent-scope memory requires an agent identity")
        return ctx.agent_id
    if scope == "team":
        return _TEAM_OWNER
    if scope == "topic":
        return _TOPIC_OWNER
    raise ValueError(f"invalid memory scope {scope!r}; expected one of {list(_SCOPES)}")


class MemoryAppendTool(Tool):
    """Save a durable memory into a compartment."""

    @classmethod
    def name(cls) -> str:
        return "memory_append"

    @classmethod
    def description(cls) -> str:
        return (
            "Save a durable memory you can recall later. Use for decisions, "
            "facts, preferences, and outcomes worth remembering across sessions. "
            "Memories carry a weight and decay over time; recall the most salient "
            "with memory_query."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "memory_append",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The fact, decision, or preference to remember.",
                        },
                        "scope": {
                            "type": "string",
                            "enum": list(_SCOPES),
                            "description": (
                                "agent = your own private memory (default); "
                                f"team = shared team memory (use name "
                                f"'{ORG_CONTEXT_COMPARTMENT}' for the standing org "
                                "blackboard); topic = a shared named topic (use name "
                                "'initiative:<id>' to log an initiative's "
                                "status/blockers/decisions and recall it before deciding)."
                            ),
                            "default": "agent",
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Compartment name, e.g. 'notes', 'user-profile', "
                                "or a topic slug. Default 'notes'."
                            ),
                            "default": "notes",
                        },
                        "weight": {
                            "type": "number",
                            "description": "Salience 0.3 (background) .. 3.0 (critical); default 1.0.",
                        },
                    },
                    "required": ["content"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        content = args.get("content")
        if not content:
            return json.dumps({"error": "missing required 'content' argument"})
        scope = args.get("scope", "agent")
        name = args.get("name", "notes")
        weight = args.get("weight", 1.0)
        try:
            owner = _resolve_owner(scope, ctx)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        try:
            memory_id = provider.write(
                scope=scope,
                owner=owner,
                name=name,
                content=content,
                weight=float(weight),
                source_conversation_id=ctx.conversation_id,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"memory_id": memory_id, "scope": scope, "compartment": name})


class MemoryQueryTool(Tool):
    """Recall memories from a compartment, ranked by decayed salience."""

    @classmethod
    def name(cls) -> str:
        return "memory_query"

    @classmethod
    def description(cls) -> str:
        return (
            "Recall durable memories from a compartment, ranked by salience with "
            "stale memories decayed out. Use to remember prior decisions, facts, "
            "and preferences before acting."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "memory_query",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords describing what to recall.",
                        },
                        "scope": {
                            "type": "string",
                            "enum": list(_SCOPES),
                            "description": "agent (default) / team / topic.",
                            "default": "agent",
                        },
                        "name": {
                            "type": "string",
                            "description": "Compartment name. Default 'notes'.",
                            "default": "notes",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10).",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args: dict[str, Any] = json.loads(arguments)
        query = args.get("query")
        if not query:
            return json.dumps({"error": "missing required 'query' argument"})
        scope = args.get("scope", "agent")
        name = args.get("name", "notes")
        limit = args.get("limit", 10)
        try:
            owner = _resolve_owner(scope, ctx)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        try:
            hits = provider.recall(
                scope=scope, owner=owner, name=name, query=query, k=int(limit)
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        # Out-of-band reinforcement: the provider records recalled ids for a
        # batched, off-path flush (T8). This is in-memory only — the recall above
        # stayed a pure DB read; no last_accessed_at / access_count write happens
        # inline. Routed through the port (BDP-2369) — no reach-through to db.utils
        # / the reinforcement-buffer module.
        provider.note_recalled(hits)
        results = [
            {
                "content": hit.content,
                "weight": round(hit.effective_weight, 4),
                "memory_id": hit.id,
            }
            for hit in hits
        ]
        if not results:
            return json.dumps({"results": [], "message": "No matching memories."})
        return json.dumps({"results": results})


class MemoryCompartmentsListTool(Tool):
    """List the caller's reachable memory compartments."""

    @classmethod
    def name(cls) -> str:
        return "memory_compartments_list"

    @classmethod
    def description(cls) -> str:
        return (
            "List your reachable memory compartments — your private agent "
            "compartments plus the shared team and topic compartments."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "memory_compartments_list",
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments
        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        comps: list[dict[str, Any]] = []
        if ctx.agent_id:
            comps += provider.list_compartments(scope="agent", owner=ctx.agent_id)
        comps += provider.list_compartments(scope="team")
        comps += provider.list_compartments(scope="topic")
        out = [{"scope": c["scope"], "name": c["name"]} for c in comps]
        # Always surface the standing org blackboard (BDP-2276 D6/E1) — the
        # store only lists compartments that hold a row, so an unwritten
        # org-context would otherwise be invisible and undiscoverable.
        out = ensure_org_compartments(out)
        return json.dumps({"compartments": out})
