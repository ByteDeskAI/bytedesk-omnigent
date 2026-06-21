"""Deterministic pilot plans for catalog integration rollout.

The catalog describes what an integration can unlock; verification matrices define
acceptance gates. Pilot plans bridge those surfaces into the first tenant-safe
rollout shape that ByteDesk Platform or an autonomous planning loop can present
before activating a connector.
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

IntegrationPilotTier = Literal["internal_harness", "external_read", "external_write"]


@dataclass(frozen=True)
class IntegrationPilotPlan:
    """A JSON-ready rollout pilot plan for one catalog capability."""

    capability_slug: str
    capability_name: str
    category: CapabilityCategory
    pilot_tier: IntegrationPilotTier
    pilot_boundaries: tuple[str, ...]
    recommended_stakeholders: tuple[str, ...]
    success_metrics: tuple[str, ...]
    exit_criteria: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["object"] = "integration_pilot_plan"
        for key in (
            "pilot_boundaries",
            "recommended_stakeholders",
            "success_metrics",
            "exit_criteria",
        ):
            data[key] = list(data[key])
        return data


def compile_integration_pilot_plan(slug: str) -> IntegrationPilotPlan | None:
    """Return the tenant-safe pilot rollout plan for a catalog capability."""

    capability = get_integration_capability(slug)
    verification_matrix = compile_integration_verification_matrix(slug)
    if capability is None or verification_matrix is None:
        return None

    pilot_tier = verification_matrix["risk_tier"]
    return IntegrationPilotPlan(
        capability_slug=capability.slug,
        capability_name=capability.name,
        category=capability.category,
        pilot_tier=pilot_tier,
        pilot_boundaries=_pilot_boundaries(pilot_tier),
        recommended_stakeholders=_recommended_stakeholders(
            capability.category, pilot_tier
        ),
        success_metrics=_success_metrics(capability.category, pilot_tier),
        exit_criteria=_exit_criteria(pilot_tier),
    )


def _pilot_boundaries(pilot_tier: IntegrationPilotTier) -> tuple[str, ...]:
    if pilot_tier == "internal_harness":
        return (
            "no external tenant credentials required",
            "runs only against checked-in blueprint fixtures",
            "phase mutations are recorded as inert evidence events",
        )
    if pilot_tier == "external_read":
        return (
            "single sandbox workspace or tenant only",
            "read-only provider scopes unless explicitly re-approved",
            "no provider-side mutations during the pilot window",
        )
    return (
        "single sandbox workspace or tenant only",
        "all outbound writes require explicit operator approval",
        "provider-side mutations are limited to reversible test objects",
    )


def _recommended_stakeholders(
    category: CapabilityCategory, pilot_tier: IntegrationPilotTier
) -> tuple[str, ...]:
    if pilot_tier == "internal_harness":
        return (
            "platform engineering owner",
            "agent operations lead",
            "workflow template reviewer",
        )

    stakeholders = ["platform integration owner", "security reviewer"]
    if category in {"communication", "crm_support", "commerce_billing"}:
        stakeholders.append("customer success pilot owner")
    elif category == "developer":
        stakeholders.append("engineering workflow owner")
    else:
        stakeholders.append("tenant operations owner")
    return tuple(stakeholders)


def _success_metrics(
    category: CapabilityCategory, pilot_tier: IntegrationPilotTier
) -> tuple[str, ...]:
    if pilot_tier == "internal_harness":
        return (
            "at least 3 deterministic workflow blueprint dry runs complete without manual repair",
            "100% of phase outputs include typed evidence references",
            "operator can replay one failed phase from stored inputs",
        )

    category_metric = {
        "communication": "agent messages are correlated to source thread and task ids",
        "project_management": (
            "external work-item status maps to Omnigent task lifecycle without drift"
        ),
        "knowledge": "read and write evidence preserves source document provenance",
        "developer": "repository events create reviewable Omnigent task evidence",
        "crm_support": "customer records include before-and-after audit summaries",
        "commerce_billing": "revenue events are visible without unauthorized financial mutation",
        "workflow_harness": "workflow phase evidence remains replayable",
    }[category]
    metrics = [
        "pilot processes at least 10 representative provider events",
        category_metric,
        "operator-facing status excludes credentials and sensitive payloads",
    ]
    if pilot_tier == "external_write":
        metrics.append("100% of mutating actions include approval evidence")
    return tuple(metrics)


def _exit_criteria(pilot_tier: IntegrationPilotTier) -> tuple[str, ...]:
    criteria = [
        "verification matrix gates have named evidence owners",
        "rollback or disablement path is rehearsed once",
        "known gaps are captured as follow-up tasks",
    ]
    if pilot_tier != "internal_harness":
        criteria.append("business value owner signs off on GA readiness")
    return tuple(criteria)
