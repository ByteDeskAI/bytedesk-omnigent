"""Deterministic acceptance suites for integration capability rollout.

Verification matrices describe the gates an integration must satisfy. This module
turns those gates into runnable, deterministic scenario manifests that a harness,
planning agent, or ByteDesk Platform UI can use before enabling a connector for a
tenant. The suite is pure and secret-free: it compiles from catalog metadata only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

AcceptanceMode = Literal[
    "contract",
    "auth_boundary",
    "happy_path",
    "replay",
    "policy_gate",
    "fail_closed",
]


@dataclass(frozen=True)
class AcceptanceScenario:
    """One deterministic scenario a rollout harness should prove."""

    id: str
    title: str
    mode: AcceptanceMode
    expected_evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expected_evidence"] = list(self.expected_evidence)
        return data


_CATEGORY_SCENARIOS: dict[CapabilityCategory, AcceptanceScenario] = {
    "communication": AcceptanceScenario(
        id="communication-loop-is-auditable",
        title="Human collaboration loop is auditable",
        mode="happy_path",
        expected_evidence=(
            "source thread or channel id is preserved on the Omnigent signal",
            "agent reply evidence includes task id and actor context",
            "workspace or channel rate-limit key is recorded",
        ),
    ),
    "project_management": AcceptanceScenario(
        id="work-item-lifecycle-round-trips",
        title="External work item lifecycle round-trips safely",
        mode="happy_path",
        expected_evidence=(
            "create, update, block, and complete states map to stable task events",
            "comment or checklist write-back preserves external author attribution",
            "human source-of-truth conflicts produce a blocked task signal",
        ),
    ),
    "knowledge": AcceptanceScenario(
        id="knowledge-scope-is-proven",
        title="Knowledge access scope and provenance are proven",
        mode="happy_path",
        expected_evidence=(
            "read fixture is limited to selected files, pages, or databases",
            "write fixture records provenance back to source task and agent",
            "broad search or mailbox access is rejected without approval evidence",
        ),
    ),
    "developer": AcceptanceScenario(
        id="developer-change-is-review-safe",
        title="Developer automation remains review-safe",
        mode="policy_gate",
        expected_evidence=(
            "repository fixture uses installation-scoped least-privilege access",
            "code or CI mutation is represented as a reviewable pull request",
            "failed checks and review comments are attached as task evidence",
        ),
    ),
    "crm_support": AcceptanceScenario(
        id="customer-record-update-is-governed",
        title="Customer-facing record updates are governed",
        mode="policy_gate",
        expected_evidence=(
            "public reply fixture requires approval before customer-visible write",
            "record update fixture captures before and after summaries",
            "support-to-sales handoff fixture preserves customer context and consent",
        ),
    ),
    "commerce_billing": AcceptanceScenario(
        id="revenue-mutation-is-risk-tiered",
        title="Revenue-affecting mutation is risk-tiered",
        mode="policy_gate",
        expected_evidence=(
            "refund, cancellation, or billing mutation fixture requires approval",
            "read-only revenue context fixture has no payment-side effects",
            "financial anomaly fixture includes source object links",
        ),
    ),
    "workflow_harness": AcceptanceScenario(
        id="workflow-happy-path",
        title="Workflow blueprint compiles into deterministic task graph",
        mode="happy_path",
        expected_evidence=(
            "stable phase node ids are preserved from input to compiled task graph",
            "typed phase inputs and outputs are present for every compiled node",
            "terminal completion evidence names the responsible agent role",
        ),
    ),
}


def _base_scenarios() -> tuple[AcceptanceScenario, ...]:
    return (
        AcceptanceScenario(
            id="catalog-contract-loads",
            title="Catalog contract loads without network or secrets",
            mode="contract",
            expected_evidence=(
                "capability slug resolves from the static integration catalog",
                "auth model, required scopes, and business case are non-empty",
                "compiled response contains no credentials or tenant identifiers",
            ),
        ),
        AcceptanceScenario(
            id="auth-boundary-is-declared",
            title="Authorization boundary is declared before execution",
            mode="auth_boundary",
            expected_evidence=(
                "requested scopes match the catalog entry exactly",
                "credential storage path is represented as an inert fixture",
                "refresh, re-authorization, or no-auth path is documented",
            ),
        ),
    )


def _provider_scenarios(category: CapabilityCategory) -> tuple[AcceptanceScenario, ...]:
    if category == "workflow_harness":
        return (
            _CATEGORY_SCENARIOS[category],
            AcceptanceScenario(
                id="workflow-phase-fail-closed",
                title="Workflow phase failure stops downstream mutation",
                mode="fail_closed",
                expected_evidence=(
                    "failed phase records terminal evidence and reason",
                    "downstream write phases are skipped after failed prerequisite",
                    "operator-visible recovery hint names the failed phase id",
                ),
            ),
        )

    return (
        AcceptanceScenario(
            id="provider-event-normalizes",
            title="Provider event normalizes into Omnigent signal",
            mode="happy_path",
            expected_evidence=(
                "external event id is preserved for traceability",
                "tenant or workspace routing key is present",
                "unsupported event fields are ignored without leaking secrets",
            ),
        ),
        AcceptanceScenario(
            id="provider-delivery-replays-idempotently",
            title="Duplicate provider delivery is idempotent",
            mode="replay",
            expected_evidence=(
                "idempotency key is derived from stable provider identifiers",
                "duplicate fixture returns the same normalized outcome",
                "terminal failure path declares retry and dead-letter behavior",
            ),
        ),
        AcceptanceScenario(
            id="provider-write-is-policy-gated",
            title="Provider write is policy-gated",
            mode="policy_gate",
            expected_evidence=(
                "read fixture is separate from write fixture",
                "high-risk write names the required approval strategy",
                "denied approval leaves provider state unchanged",
            ),
        ),
        _CATEGORY_SCENARIOS[category],
    )


def compile_integration_acceptance_suite(slug: str) -> dict | None:
    """Return a JSON-ready deterministic acceptance suite for a capability."""

    capability = get_integration_capability(slug)
    matrix = compile_integration_verification_matrix(slug)
    if capability is None or matrix is None:
        return None

    scenarios = (*_base_scenarios(), *_provider_scenarios(capability.category))
    scenario_dicts = [scenario.to_dict() for scenario in scenarios]
    return {
        "object": "integration_acceptance_suite",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "provider_category": capability.category,
        "risk_tier": matrix["risk_tier"],
        "auth_model": capability.auth_model,
        "required_scopes": list(capability.required_scopes),
        "minimum_passing_scenarios": len(scenario_dicts),
        "scenarios": scenario_dicts,
    }
