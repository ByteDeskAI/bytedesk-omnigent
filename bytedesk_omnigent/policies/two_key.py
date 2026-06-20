"""Built-in two-key approval gate (BDP-2277 F2, ADR-0142).

The two-person rule for irreversible actions: a tool call matching a high-risk
pattern needs **N distinct human approvers** (default 2), not one. A single
sign-off is never enough — no lone operator (or a compromised one) can push an
irreversible action through.

The policy reads the distinct approver identities recorded in
``session_state[_APPROVERS_KEY]`` (the approval subsystem appends each approver's
identity as they sign off) and ALLOWs once ``>= min_approvers`` distinct are
present, otherwise ASKs for another. Until the approver-recording seam is wired
the safe default holds: a matched call always ASKs (it can never auto-allow).
Stateless evaluation over ``session_state``, mirroring ``spawn_governor``.
"""

from __future__ import annotations

import re

from bytedesk_omnigent.policies import PolicyRegistryRaw
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# session_state key holding the list of distinct approver identities recorded for
# the pending high-risk action (appended by the approval subsystem on each sign-off).
_APPROVERS_KEY = "_policy_two_key_approvers"


def two_key_required(patterns: list[str], min_approvers: int = 2) -> PolicyCallable:
    """Factory: require ``min_approvers`` distinct approvers for a matched tool call.

    :param patterns: Regex patterns (``re.search`` against the tool name) of
        irreversible actions that need the two-person rule.
    :param min_approvers: Distinct approvers required before ALLOW. Default 2.
    :returns: A policy callable that ALLOWs once enough distinct approvers are
        recorded, else ASKs.
    """
    compiled = [re.compile(p) for p in patterns]

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        name = data.get("name", "")
        if not any(pat.search(name) for pat in compiled):
            return _ALLOW
        state = event.get("session_state") or {}
        recorded = state.get(_APPROVERS_KEY) or []
        distinct = {a for a in recorded if a}
        if len(distinct) >= min_approvers:
            return _ALLOW
        return {
            "result": "ASK",
            "reason": (
                f"two-key gate: '{name}' requires {min_approvers} distinct "
                f"approvers; {len(distinct)} recorded — needs another sign-off "
                "(ADR-0142)."
            ),
        }

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.two_key.two_key_required",
        "kind": "factory",
        "name": "Two-Key Approval Gate",
        "description": "Requires N distinct human approvers (default 2) before a "
        "matched irreversible tool call runs — the two-person rule (ADR-0142).",
        "params_schema": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns of irreversible tool names",
                },
                "min_approvers": {
                    "type": "integer",
                    "description": "Distinct approvers required before ALLOW",
                    "default": 2,
                },
            },
            "required": ["patterns"],
        },
    },
]
