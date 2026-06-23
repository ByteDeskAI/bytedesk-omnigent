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

from bytedesk_omnigent.policies import PolicyRegistryRaw
from bytedesk_omnigent.policies._floors import (
    SPAWN_BREADTH_SANITY,
    require_int_in_range,
)
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# The new-child spawn tool. Matched bare or under any MCP/native prefix.
_SPAWN_TOOL = "sys_session_create"
_COUNTER_KEY = "_policy_spawn_count"
# Per-task spawn counters (BDP-2338) live under this prefix + the task id, so a
# task-tagged spawn is bounded independently of the session-wide breadth cap.
_TASK_COUNTER_PREFIX = "_policy_spawn_count_task:"


def _is_spawn(name: str) -> bool:
    return name == _SPAWN_TOOL or name.endswith(_SPAWN_TOOL)


def spawn_breadth_governor(
    max_spawns: int = 16,
    *,
    per_task_max_spawns: int | None = None,
) -> PolicyCallable:
    """Factory: deny after *max_spawns* child-session spawns in the session.

    :param max_spawns: Maximum ``sys_session_create`` calls allowed across the
        session. Defaults to 16 — a conservative backstop (the prior harness
        ``MAX_FANOUT_ITEMS`` was 64); raise it per trusted orchestrator.
    :param per_task_max_spawns: Optional per-first-class-task spawn cap
        (BDP-2338). When set, a ``sys_session_create`` attributed to a task
        (``arguments.task_id``) is ALSO counted per task and DENYed once this
        many spawns exist for that task — reusing the breadth-cap mechanism.
        ``None`` (default) keeps the legacy breadth-only behavior.
    :returns: A policy callable that DENYs a spawn over the limit.
    :raises PolicyFloorError: if a cap is negative or effectively-infinite (an
        unbounded cap defeats the runaway-fan-out backstop). ``0`` is allowed —
        it disables spawning, the restrictive (safe) direction.
    """
    max_spawns = require_int_in_range("max_spawns", max_spawns, 0, SPAWN_BREADTH_SANITY)
    if per_task_max_spawns is not None:
        per_task_max_spawns = require_int_in_range(
            "per_task_max_spawns", per_task_max_spawns, 0, SPAWN_BREADTH_SANITY
        )

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
        # Optional per-task cap (BDP-2338): reuse the breadth-cap counter
        # mechanism, keyed per first-class task. Only applies when configured AND
        # the spawn is attributed to a task via arguments.task_id.
        if per_task_max_spawns is not None:
            task_id = (data.get("arguments") or {}).get("task_id")
            if task_id:
                task_key = f"{_TASK_COUNTER_PREFIX}{task_id}"
                task_count = int(state.get(task_key, 0))
                if task_count >= per_task_max_spawns:
                    return {
                        "result": "DENY",
                        "reason": (
                            f"spawn-breadth governor (per-task): task '{task_id}' "
                            f"exceeded {per_task_max_spawns} child sessions "
                            "(per-task fan-out backstop, ADR-0142)"
                        ),
                    }
                return {
                    "result": "ALLOW",
                    "state_updates": [
                        {"key": _COUNTER_KEY, "action": "increment", "value": 1},
                        {"key": task_key, "action": "increment", "value": 1},
                    ],
                }
        return {
            "result": "ALLOW",
            "state_updates": [
                {"key": _COUNTER_KEY, "action": "increment", "value": 1},
            ],
        }

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.spawn_governor.spawn_breadth_governor",
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
                "per_task_max_spawns": {
                    "type": ["integer", "null"],
                    "description": "Optional per-first-class-task spawn cap; "
                    "counts sys_session_create per arguments.task_id. "
                    "Null/omitted = session-wide breadth cap only.",
                    "default": None,
                },
            },
            "required": ["max_spawns"],
        },
    },
]
