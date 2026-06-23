"""Built-in forever-gated tool registry (BDP-2271 F7, ADR-0142).

A server-tier deny-list: tool-name patterns that **no agent may ever call**, no
matter its grants — the enforced floor under "what must stay human-gated" (prod
deploys go through TeamCity only; prod billing mutations; destructive infra). A
compromised or hallucinating agent is physically unable to trip these: the
verdict is an unconditional **DENY** (evaluated server-tier, last — DENY
short-circuits), never an ASK. Belt-and-suspenders with omitting these tools from
every agent's native allowlist.

Patterns are regex, matched with ``re.search`` against the tool name on the
``tool_call`` phase. Stateless, like ``spawn_governor`` / ``safety``.
"""

from __future__ import annotations

import re

from bytedesk_omnigent.policies import PolicyRegistryRaw
from bytedesk_omnigent.policies._floors import PolicyFloorError
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}


def forever_denied(patterns: list[str]) -> PolicyCallable:
    """Factory: deny any tool call whose name matches a forever-denied pattern.

    :param patterns: Regex patterns (``re.search`` against the tool name) that are
        never permitted, e.g.
        ``["promote\\.production", "deploy\\.run", "billing\\.(refund|charge)"]``.
    :returns: A policy callable that DENYs a matching tool call.
    :raises PolicyFloorError: if any pattern is not a valid regex — a deny-list
        rule that cannot compile would silently never match (fail-open), so the
        gate fails closed at construction instead.
    """
    # NOTE (BDP-2411): the "baseline patterns un-removable / removal requires
    # two-key" floor is enforced on the config write path (BDP-2414), which knows
    # baseline-vs-operator provenance; the factory only fails closed on a bad regex.
    try:
        compiled = [re.compile(p) for p in patterns]
    except re.error as exc:
        raise PolicyFloorError(
            f"forever_denied pattern does not compile ({exc}) — refusing to attach "
            "a deny-list rule that can never match"
        ) from exc

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        name = data.get("name", "")
        for pat in compiled:
            if pat.search(name):
                return {
                    "result": "DENY",
                    "reason": (
                        f"tool '{name}' is forever-denied (matched /{pat.pattern}/) — a "
                        "human-gated boundary (ADR-0142); no agent may ever run this"
                    ),
                }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.forever_gate.forever_denied",
        "kind": "factory",
        "name": "Forever-Denied Tool Registry",
        "description": "Denies any tool call matching a forbidden regex pattern — the "
        "server-tier floor for human-gated boundaries (prod deploy, prod billing, "
        "destructive infra), ADR-0142.",
        "params_schema": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns of tool names no agent may ever call",
                },
            },
            "required": ["patterns"],
        },
    },
]
