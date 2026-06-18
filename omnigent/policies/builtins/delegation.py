"""Built-in delegation-graph authority gate (BDP-2269 C1, ADR-0142).

Turns the displayed org chart (``params.managers`` → each agent's direct reports)
into **runtime** spawn/delegation authorization: an agent may only spawn
(``sys_session_create``) a **named** target that is one of its allowed delegation
targets (its direct reports). A named target outside its sub-tree is DENIED — the
org chart is enforced, not decorative. The allowed set is derived from
``params.managers`` at policy-attach time and passed as ``factory_params``, so the
policy stays a stateless name check (mirrors ``spawn_governor`` /
``forever_gate``). Un-named (local-bundle) spawns are out of scope here — they are
governed by the spawn-breadth governor (C5) + the forever-gated registry (F7).
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

_SPAWN_TOOL = "sys_session_create"


def _is_spawn(name: str) -> bool:
    return name == _SPAWN_TOOL or name.endswith(_SPAWN_TOOL)


def delegation_authority(allowed_targets: list[str]) -> PolicyCallable:
    """Factory: DENY a spawn whose named target is not an allowed delegation target.

    :param allowed_targets: Agent names/ids this agent may delegate to (its direct
        reports, derived from ``params.managers``).
    :returns: A policy callable that DENYs a ``sys_session_create`` whose
        ``agent_name``/``agent_id`` argument is outside ``allowed_targets``.
    """
    allowed = {t for t in allowed_targets if t}

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        if not _is_spawn(data.get("name", "")):
            return _ALLOW
        args = data.get("arguments") or {}
        target = args.get("agent_name") or args.get("agent_id")
        # Un-named (local-bundle) spawn — out of scope for the delegation graph.
        if not target:
            return _ALLOW
        if target in allowed:
            return _ALLOW
        return {
            "result": "DENY",
            "reason": (
                f"delegation-graph: '{target}' is not a delegation target of this "
                f"agent (allowed: {sorted(allowed)}) — the org chart is enforced "
                "(ADR-0142)"
            ),
        }

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.delegation.delegation_authority",
        "kind": "factory",
        "name": "Delegation-Graph Authority",
        "description": "Denies a sys_session_create whose named target is not one of "
        "the agent's allowed delegation targets (its reports per params.managers) — "
        "runtime enforcement of the org chart (ADR-0142).",
        "params_schema": {
            "type": "object",
            "properties": {
                "allowed_targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent names/ids this agent may delegate to",
                },
            },
            "required": ["allowed_targets"],
        },
    },
]
