"""Business value scorecards for integration capability prioritization.

The static catalog says what Omnigent can build next. This module turns a catalog
entry into a deterministic, JSON-ready scorecard that product, sales, and
autonomous planning agents can use to explain why a connector should be enabled
or built for a tenant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)


@dataclass(frozen=True)
class ScoreDimension:
    """One explainable value dimension for an integration capability."""

    score: int
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)


def compile_integration_value_scorecard(slug: str) -> dict | None:
    """Return a deterministic business-value scorecard for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    verification = compile_integration_verification_matrix(slug)
    risk_tier = verification["risk_tier"] if verification else "external_read"
    dimensions = _dimensions(capability.category, capability.priority_score, risk_tier)
    overall_score = round(
        dimensions["agent_autonomy"].score * 0.35
        + dimensions["buyer_pull"].score * 0.30
        + dimensions["time_to_value"].score * 0.20
        + dimensions["operational_safety"].score * 0.15
    )

    return {
        "object": "integration_value_scorecard",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "overall_score": overall_score,
        "dimensions": {key: value.to_dict() for key, value in dimensions.items()},
        "recommended_sales_motion": _recommended_sales_motion(capability.category),
        "required_enablement": _required_enablement(risk_tier),
        "business_case": capability.business_case,
        "future_unlocks": list(capability.future_unlocks),
    }


def _dimensions(
    category: CapabilityCategory, priority_score: int, risk_tier: str
) -> dict[str, ScoreDimension]:
    autonomy = 100 if category == "workflow_harness" else min(98, priority_score)
    buyer_pull = _buyer_pull_score(category, priority_score)
    time_to_value = 92 if category in {"communication", "project_management"} else 86
    if category == "workflow_harness":
        time_to_value = 94
    operational_safety = 96 if risk_tier == "internal_harness" else 88
    if risk_tier == "external_write":
        operational_safety = 82

    return {
        "agent_autonomy": ScoreDimension(
            score=autonomy,
            rationale=(
                "Measures how directly the capability lets agents create, coordinate, "
                "or complete work without manual handoffs."
            ),
        ),
        "buyer_pull": ScoreDimension(
            score=buyer_pull,
            rationale=(
                "Estimates how commonly target customers already depend on this "
                "system or workflow category."
            ),
        ),
        "time_to_value": ScoreDimension(
            score=time_to_value,
            rationale=(
                "Rewards capabilities that can show visible user value before "
                "deep back-office rollout."
            ),
        ),
        "operational_safety": ScoreDimension(
            score=operational_safety,
            rationale=(
                "Reflects how safely the capability can be enabled with deterministic "
                "gates, approvals, and rollback paths."
            ),
        ),
    }


def _buyer_pull_score(category: CapabilityCategory, priority_score: int) -> int:
    category_floor = {
        "communication": 94,
        "project_management": 93,
        "knowledge": 90,
        "developer": 92,
        "crm_support": 88,
        "commerce_billing": 84,
        "workflow_harness": 96,
    }[category]
    return max(category_floor, min(99, priority_score))


def _recommended_sales_motion(category: CapabilityCategory) -> list[str]:
    if category == "workflow_harness":
        return [
            "Sell as deterministic autonomous-workflow infrastructure for "
            "repeatable agent factories.",
            "Lead with executive visibility into phases, gates, and completion evidence.",
        ]
    if category in {"communication", "project_management"}:
        return [
            "Start with a team-level pilot in the customer's existing work surface.",
            "Demonstrate status sync, approvals, and agent handoffs before expanding scope.",
        ]
    return [
        "Start read-only with explicit workspace or object selection.",
        "Expand to writes only after verification gates produce clean evidence.",
    ]


def _required_enablement(risk_tier: str) -> list[str]:
    enablement = [
        "catalog entry reviewed with tenant owner",
        "verification matrix accepted by operator",
        "rollback owner and disablement path named",
    ]
    if risk_tier == "external_write":
        enablement.append("approval policy configured for provider-side mutations")
    elif risk_tier == "external_read":
        enablement.append("least-privilege read scopes confirmed before OAuth authorization")
    else:
        enablement.append("workflow phase evidence schema reviewed before template publication")
    return enablement
