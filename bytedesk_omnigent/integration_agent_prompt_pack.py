"""Deterministic agent prompt packs for integration capabilities.

The integration catalog describes what to build, while verification matrices
describe rollout gates. This module combines both into a JSON-ready prompt pack
that agent-creation flows can use to instantiate a safe, provider-aware
integration agent without live credentials or network calls.
"""

from __future__ import annotations

from bytedesk_omnigent.integration_capabilities import get_integration_capability
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

_ALLOWED_ACTIONS_BY_RISK_TIER: dict[str, list[str]] = {
    "internal_harness": [
        "compile workflow blueprints into Omnigent Tasks",
        "validate phase inputs, outputs, retry policy, and completion evidence",
        "emit operator-visible dry-run plans before execution",
    ],
    "external_read": [
        "read provider objects within the granted scope boundary",
        "normalize provider context into Omnigent signals and Tasks",
        "emit source-linked evidence for every imported object",
    ],
    "external_write": [
        "read provider objects within the granted scope boundary",
        "draft provider-side updates with source-linked evidence",
        "execute approved low-risk writes through the connector tool facade",
    ],
}

_BLOCKED_ACTIONS_BY_RISK_TIER: dict[str, list[str]] = {
    "internal_harness": [
        "execute undeclared phases",
        "skip required verification evidence",
        "mutate external systems without an explicit connector capability",
    ],
    "external_read": [
        "request scopes outside the catalog contract",
        "write to provider systems",
        "persist provider data without provenance",
    ],
    "external_write": [
        "request scopes outside the catalog contract",
        "write to provider systems without outcome evidence",
        "request human approval before provider-side writes",
    ],
}

_AUTONOMY_MODE_BY_RISK_TIER: dict[str, str] = {
    "internal_harness": "deterministic_harness",
    "external_read": "read_only_observer",
    "external_write": "approval_gated_write",
}


def compile_integration_agent_prompt_pack(slug: str) -> dict | None:
    """Return a JSON-ready prompt pack for creating one integration agent."""

    capability = get_integration_capability(slug)
    matrix = compile_integration_verification_matrix(slug)
    if capability is None or matrix is None:
        return None

    risk_tier = matrix["risk_tier"]
    gate_titles = [gate["title"] for gate in matrix["gates"]]
    gate_evidence = [
        evidence
        for gate in matrix["gates"]
        for evidence in gate["required_evidence"]
    ]
    role_name = f"{_role_stem(capability.slug)} Integration Agent"

    system_prompt = _compile_system_prompt(
        role_name=role_name,
        capability_name=capability.name,
        category=capability.category,
        risk_tier=risk_tier,
        auth_model=capability.auth_model,
        required_scopes=list(capability.required_scopes),
        implementation_description=capability.implementation_description,
        business_case=capability.business_case,
        gate_titles=gate_titles,
        gate_evidence=gate_evidence,
    )

    return {
        "object": "integration_agent_prompt_pack",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "agent_blueprint": {
            "role_name": role_name,
            "category": capability.category,
            "risk_tier": risk_tier,
            "autonomy_mode": _AUTONOMY_MODE_BY_RISK_TIER[risk_tier],
            "auth_model": capability.auth_model,
            "required_scopes": list(capability.required_scopes),
            "allowed_actions": _ALLOWED_ACTIONS_BY_RISK_TIER[risk_tier],
            "blocked_actions": _BLOCKED_ACTIONS_BY_RISK_TIER[risk_tier],
            "success_outcomes": list(capability.future_unlocks),
        },
        "verification_gate_ids": [gate["id"] for gate in matrix["gates"]],
        "system_prompt": system_prompt,
    }


def _compile_system_prompt(
    *,
    role_name: str,
    capability_name: str,
    category: str,
    risk_tier: str,
    auth_model: str,
    required_scopes: list[str],
    implementation_description: str,
    business_case: str,
    gate_titles: list[str],
    gate_evidence: list[str],
) -> str:
    scopes = ", ".join(required_scopes) if required_scopes else "no external scopes"
    gates = "; ".join(gate_titles)
    evidence = "; ".join(gate_evidence)
    return (
        f"You are the {role_name}. Your mission is to operationalize the "
        f"{capability_name} integration capability for Omnigent. Category: "
        f"{category}. Risk tier: {risk_tier}. Auth model: {auth_model}. "
        f"Allowed scope boundary: {scopes}. Implementation brief: "
        f"{implementation_description}. Business case: {business_case}. "
        f"Before claiming readiness, satisfy these verification gates: {gates}. "
        f"Required evidence includes: {evidence}. Keep all provider-side "
        f"mutations behind the declared autonomy mode and preserve task, agent, "
        f"tenant, and provider object identifiers in every outcome."
    )


def _role_stem(slug: str) -> str:
    """Return a human-readable role stem while preserving known brand styling."""

    if slug.startswith("archon-style-"):
        rest = slug.removeprefix("archon-style-").replace("-", " ").title()
        return f"Archon-Style {rest}"
    return slug.replace("-", " ").title()
