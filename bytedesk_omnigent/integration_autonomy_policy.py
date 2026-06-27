"""Deterministic autonomy policies for integration capability activation.

The integration catalog says what a connector can unlock. This module turns one
catalog entry into the safe default autonomy boundary an agent operator or
ByteDesk Platform screen can apply before live credentials or workflows are
activated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    IntegrationCapability,
    get_integration_capability,
)

IntegrationRiskTier = Literal["internal_harness", "external_read", "external_write"]
AutonomyLevel = Literal[
    "deterministic_internal",
    "observed_external_read",
    "supervised_external_write",
]


@dataclass(frozen=True)
class IntegrationAutonomyPolicy:
    """Secret-free default autonomy boundary for one integration capability."""

    capability_slug: str
    capability_name: str
    category: CapabilityCategory
    risk_tier: IntegrationRiskTier
    autonomy_level: AutonomyLevel
    requires_human_approval: bool
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    approval_required_for: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in (
            "read_scopes",
            "write_scopes",
            "allowed_actions",
            "approval_required_for",
            "forbidden_actions",
        ):
            data[key] = list(data[key])
        return data


def _is_write_scope(scope: str) -> bool:
    normalized = scope.lower()
    return (
        "write" in normalized
        or normalized.endswith((".write", ":write"))
        or normalized in {"update_content", "insert_content"}
        or "documents" in normalized
        or "spreadsheets" in normalized
        or "calendar.events" in normalized
    )


def _split_scopes(scopes: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    read_scopes: list[str] = []
    write_scopes: list[str] = []
    for scope in scopes:
        if _is_write_scope(scope):
            write_scopes.append(scope)
        else:
            read_scopes.append(scope)
    return tuple(read_scopes), tuple(write_scopes)


def _risk_tier(capability: IntegrationCapability) -> IntegrationRiskTier:
    if capability.category == "workflow_harness":
        return "internal_harness"
    if any(_is_write_scope(scope) for scope in capability.required_scopes):
        return "external_write"
    return "external_read"


def _category_approval_prompts(capability: IntegrationCapability) -> tuple[str, ...]:
    prompts: dict[CapabilityCategory, tuple[str, ...]] = {
        "communication": (
            f"posting outbound messages to {capability.name}",
            "inviting or mentioning humans in provider workspaces",
        ),
        "project_management": (
            "creating, closing, or reprioritizing external work items",
            "writing status back to a human-owned project tracker",
        ),
        "knowledge": (
            "broad knowledge access beyond selected files, pages, or datasets",
            "publishing or overwriting shared knowledge artifacts",
        ),
        "developer": (
            "opening pull requests, changing code, or triggering release workflows",
            "commenting on reviews or altering issue state",
        ),
        "crm_support": (
            "sending public customer replies",
            "changing customer, deal, or ticket records",
        ),
        "commerce_billing": (
            "issuing refunds, cancellations, or billing mutations",
            "changing order, subscription, or payment state",
        ),
        "workflow_harness": (),
    }
    return prompts[capability.category]


def compile_integration_autonomy_policy(slug: str) -> dict | None:
    """Return a JSON-ready safe autonomy policy for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability)
    read_scopes, write_scopes = _split_scopes(capability.required_scopes)
    if risk_tier == "internal_harness":
        autonomy_level: AutonomyLevel = "deterministic_internal"
        requires_human_approval = False
        allowed_actions = (
            "compile workflow phases into Omnigent Tasks",
            "run deterministic dry-run validation without provider credentials",
            "capture completion evidence for harness phases",
        )
        approval_required_for: tuple[str, ...] = ()
        forbidden_actions = (
            "directly accessing third-party customer data",
            "executing undeclared workflow phases",
        )
        rationale = (
            f"{capability.name} is an internal workflow harness capability with no "
            "requested provider scopes. It can run deterministically inside Omnigent "
            "until a phase asks for an external connector."
        )
    elif risk_tier == "external_write":
        autonomy_level = "supervised_external_write"
        requires_human_approval = True
        allowed_actions = (
            "read explicitly authorized provider context",
            "draft provider-side mutations for operator review",
            "record provider object ids on Omnigent Tasks and outcome evidence",
        )
        approval_required_for = _category_approval_prompts(capability)
        forbidden_actions = (
            "mutating provider records without an Omnigent approval record",
            "requesting scopes beyond the catalog contract",
            "storing provider secrets in task payloads or logs",
        )
        rationale = (
            f"{capability.name} requests write-capable scopes ({', '.join(write_scopes)}), "
            "so autonomous agents may prepare actions but need explicit approval before "
            "provider-side mutations."
        )
    else:
        autonomy_level = "observed_external_read"
        requires_human_approval = False
        allowed_actions = (
            "read explicitly authorized provider context",
            "normalize external objects into Omnigent Tasks or signals",
            "record source links and provenance without provider mutations",
        )
        approval_required_for = _category_approval_prompts(capability)
        forbidden_actions = (
            "writing to provider records",
            "requesting scopes beyond the catalog contract",
            "storing provider secrets in task payloads or logs",
        )
        rationale = (
            f"{capability.name} only requests read-style scopes, so agents can observe "
            "and normalize data "
            "while provider-side writes remain forbidden until the catalog contract changes."
        )

    return IntegrationAutonomyPolicy(
        capability_slug=capability.slug,
        capability_name=capability.name,
        category=capability.category,
        risk_tier=risk_tier,
        autonomy_level=autonomy_level,
        requires_human_approval=requires_human_approval,
        read_scopes=read_scopes,
        write_scopes=write_scopes,
        allowed_actions=allowed_actions,
        approval_required_for=approval_required_for,
        forbidden_actions=forbidden_actions,
        rationale=rationale,
    ).to_dict()
