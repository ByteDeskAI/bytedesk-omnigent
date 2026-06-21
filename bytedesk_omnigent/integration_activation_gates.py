"""Deterministic connected-app activation gates for ByteDesk Platform.

This pure compiler answers the final setup question before a third-party app is
allowed to wake autonomous agents: is every required integration artifact ready?
It intentionally performs no network calls and reads no secrets, so Platform can
run it during connector setup, previews, and operator reviews.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_TRUE_VALUES = {"ok", "pass", "passed", "ready", "true", "yes", "enabled"}

_GATE_REASONS: dict[str, str] = {
    "secret_ready": "configure and verify the provider signing secret",
    "oauth_ready": "complete OAuth authorization for this connected app",
    "webhook_preview_passed": "run a signed webhook preview before live delivery",
    "route_configured": "bind provider events to a parked Omnigent signal",
    "replay_plan_ready": "compile a replay plan before accepting provider retries",
    "approval_policy_ready": "attach a human approval policy before provider writeback",
    "agent_handoff_ready": "compile an agent handoff package before dispatch",
}

_WORKFLOW_STEPS = [
    "normalize connected-app context",
    "verify secret and OAuth readiness",
    "prove webhook routing with a no-side-effect preview",
    "compile replay and approval safety contracts",
    "verify agent handoff package readiness",
    "enable live delivery in ByteDesk Platform",
]


def compile_integration_activation_gate(
    *,
    provider: str,
    workspace_id: str,
    connected_app_id: str,
    capabilities: Iterable[str] | None = None,
    checks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a deterministic activation decision for a connected app.

    Capabilities describe what the connector will do (for example ``webhook``,
    ``oauth``, ``writeback``, and ``agent_handoff``). Checks are setup facts
    collected by Platform or earlier deterministic compilers. The function
    returns a stable, side-effect-free activation contract that a UI or API can
    use to block premature live delivery.
    """

    provider_slug = _slug(provider)
    capability_slugs = _normalize_capabilities(capabilities)
    check_map = dict(checks or {})
    required_gates = _required_gates(capability_slugs)
    blockers = [
        {"gate": gate, "reason": _GATE_REASONS[gate]}
        for gate in required_gates
        if not _is_ready(check_map.get(gate))
    ]
    can_enable = not blockers
    return {
        "activation_id": (
            f"integration-activation:v1:{provider_slug}:{workspace_id}:{connected_app_id}"
        ),
        "provider": provider_slug,
        "workspace_id": workspace_id,
        "connected_app_id": connected_app_id,
        "capabilities": capability_slugs,
        "required_gates": required_gates,
        "status": "ready" if can_enable else "blocked",
        "can_enable": can_enable,
        "blockers": blockers,
        "next_action": blockers[0]["reason"] if blockers else "enable live delivery",
        "workflow_steps": list(_WORKFLOW_STEPS),
    }


def _required_gates(capabilities: list[str]) -> list[str]:
    gates: list[str] = []
    if "webhook" in capabilities:
        gates.extend(["secret_ready", "webhook_preview_passed", "route_configured"])
    if "oauth" in capabilities:
        gates.append("oauth_ready")
    if "writeback" in capabilities:
        gates.extend(["replay_plan_ready", "approval_policy_ready"])
    if "agent_handoff" in capabilities or not capabilities:
        gates.append("agent_handoff_ready")
    return _dedupe(gates)


def _normalize_capabilities(capabilities: Iterable[str] | None) -> list[str]:
    return _dedupe(_capability_slug(value) for value in capabilities or ["agent_handoff"])


def _capability_slug(value: object) -> str:
    return _slug(value).replace("-", "_")


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _is_ready(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return False


def _slug(value: object) -> str:
    return str(value).strip().lower().replace(" ", "-").replace("_", "-")
