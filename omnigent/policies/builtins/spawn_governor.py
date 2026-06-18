"""Built-in spawn-breadth governor (BDP-2272, ADR-0142).

Caps the number of child sessions an agent may spawn (``sys_session_create``)
within its session — a **server-side** backstop against runaway agent fan-out
before broad spawn rights are granted to the delegation graph. The org encodes
anti-loop rules in *prose* (agent instructions); the LLM can ignore them, so this
policy is the enforced guard. Combined with the native spawn-depth cap, it bounds
a runaway tree's size (replaces the harness's ``MAX_FANOUT_ITEMS=64`` constant).

Mirrors ``safety.max_tool_calls_per_session`` exactly: a ``session_state`` counter
that DENYs past the limit, incremented via ``state_updates``. It counts only
``sys_session_create`` (the unambiguous new-child signal); ordinary tool calls
and other phases pass through.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# The new-child spawn tool. Matched bare or under any MCP/native prefix.
_SPAWN_TOOL = "sys_session_create"
_COUNTER_KEY = "_policy_spawn_count"


def _is_spawn(name: str) -> bool:
    return name == _SPAWN_TOOL or name.endswith(_SPAWN_TOOL)


def spawn_breadth_governor(max_spawns: int = 16) -> PolicyCallable:
    """Factory: deny after *max_spawns* child-session spawns in the session.

    :param max_spawns: Maximum ``sys_session_create`` calls allowed across the
        session. Defaults to 16 — a conservative backstop (the prior harness
        ``MAX_FANOUT_ITEMS`` was 64); raise it per trusted orchestrator.
    :returns: A policy callable that DENYs a spawn over the limit.
    """

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        if not _is_spawn(data.get("name", "")):
            return _ALLOW
        state = event.get("session_state") or {}
        count = int(state.get(_COUNTER_KEY, 0))
        if count >= max_spawns:
            return {
                "result": "DENY",
                "reason": (
                    f"spawn-breadth governor: exceeded {max_spawns} child sessions "
                    "this session (runaway fan-out backstop, ADR-0142)"
                ),
            }
        return {
            "result": "ALLOW",
            "state_updates": [
                {"key": _COUNTER_KEY, "action": "increment", "value": 1},
            ],
        }

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.spawn_governor.spawn_breadth_governor",
        "kind": "factory",
        "name": "Spawn-Breadth Governor",
        "description": "Denies after N child-session spawns (sys_session_create) in a "
        "session — a server-side backstop against runaway agent fan-out (ADR-0142).",
        "params_schema": {
            "type": "object",
            "properties": {
                "max_spawns": {
                    "type": "integer",
                    "description": "Maximum child sessions an agent may spawn per session",
                    "default": 16,
                },
            },
            "required": ["max_spawns"],
        },
    },
]
