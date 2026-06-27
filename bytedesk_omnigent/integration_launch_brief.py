"""Deterministic launch briefs for integration capability rollout.

The verification matrix names the evidence gates. This module turns those gates
into an operator-facing launch sequence that autonomous loops and Platform UI can
use before enabling an integration for real tenants.
"""

from __future__ import annotations

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)


def compile_integration_launch_brief(slug: str) -> dict | None:
    """Return a JSON-ready launch brief for one catalog capability."""

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    risk_tier = matrix["risk_tier"]
    return {
        "object": "integration_launch_brief",
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "category": matrix["category"],
        "risk_tier": risk_tier,
        "recommended_launch_mode": _launch_mode(risk_tier),
        "authorization_plan": _authorization_plan(matrix),
        "phases": _launch_phases(matrix),
        "default_success_metric": _default_success_metric(risk_tier),
    }


def _launch_mode(risk_tier: str) -> str:
    if risk_tier == "internal_harness":
        return "internal_deterministic_harness"
    if risk_tier == "external_write":
        return "approved_pilot_then_workspace_rollout"
    return "read_only_sandbox_then_pilot"


def _authorization_plan(matrix: dict) -> dict:
    required_scopes = list(matrix["required_scopes"])
    return {
        "auth_model": matrix["auth_model"],
        "credential_posture": "no_external_credentials"
        if matrix["risk_tier"] == "internal_harness"
        else "secret_manager_required",
        "scope_review_required": bool(required_scopes),
        "required_scopes": required_scopes,
    }


def _launch_phases(matrix: dict) -> list[dict]:
    if matrix["risk_tier"] == "internal_harness":
        return [
            {
                "id": "contract",
                "title": "Freeze catalog and blueprint contract",
                "required_gates": ["catalog-contract"],
                "exit_criteria": [
                    "capability metadata resolves from the catalog",
                    "workflow inputs, outputs, and owner-facing docs are explicit",
                ],
            },
            {
                "id": "harness_dry_run",
                "title": "Run deterministic harness fixtures",
                "required_gates": [
                    "workflow-determinism",
                    "idempotency-replay",
                    "observability-evidence",
                ],
                "exit_criteria": [
                    "fixtures complete without external credentials",
                    "every phase produces stable terminal evidence",
                ],
            },
            {
                "id": "operator_review",
                "title": "Review operator-visible evidence",
                "required_gates": ["policy-approval", "rollback-readiness"],
                "exit_criteria": [
                    "operators can approve, reject, or disable the workflow",
                    "rollback preserves historical evidence",
                ],
            },
            {
                "id": "production_enablement",
                "title": "Enable as a tenant-selectable workflow template",
                "required_gates": ["observability-evidence", "rollback-readiness"],
                "exit_criteria": [
                    "template can be enabled per tenant",
                    "launch status is safe to expose without secrets",
                ],
            },
        ]

    phases = [
        {
            "id": "contract",
            "title": "Freeze catalog, provider, and event contract",
            "required_gates": ["catalog-contract"],
            "exit_criteria": [
                "provider object ids and tenant routing fields are named",
                "business case and rollout owner are documented",
            ],
        },
        {
            "id": "oauth_sandbox",
            "title": "Connect sandbox authorization boundary",
            "required_gates": ["auth-boundary", "rollback-readiness"],
            "exit_criteria": [
                "requested scopes exactly match the catalog",
                "credentials are stored in the configured secret manager",
            ],
        },
        {
            "id": "read_only_pilot",
            "title": "Validate read-only ingress and replay behavior",
            "required_gates": [
                "ingress-normalization",
                "idempotency-replay",
                "observability-evidence",
            ],
            "exit_criteria": [
                "external events normalize into deterministic Omnigent signals",
                "duplicate deliveries return the same normalized outcome",
            ],
        },
    ]

    if matrix["risk_tier"] == "external_write":
        phases.append(
            {
                "id": "approved_write_pilot",
                "title": "Pilot policy-gated provider writes",
                "required_gates": ["policy-approval", matrix["gates"][-1]["id"]],
                "exit_criteria": [
                    "mutating provider actions require approval and leave outcome records",
                    "denied approvals produce no provider-side mutation",
                ],
            }
        )

    phases.append(
        {
            "id": "production_enablement",
            "title": "Enable controlled tenant rollout",
            "required_gates": ["observability-evidence", "rollback-readiness"],
            "exit_criteria": [
                "connector can be disabled without deleting evidence",
                "operator status contains no secrets or raw tokens",
            ],
        }
    )
    return phases


def _default_success_metric(risk_tier: str) -> str:
    if risk_tier == "internal_harness":
        return "100% of workflow phases emit terminal evidence in dry-run fixtures"
    if risk_tier == "external_write":
        return "0 unapproved provider mutations during pilot with 100% outcome evidence coverage"
    return "100% of sampled provider events normalize without mutation during sandbox pilot"
