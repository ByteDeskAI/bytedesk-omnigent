"""Deterministic dependency graphs for integration capability delivery.

The verification matrix says how to prove a connector is production-ready. This
module answers the earlier planning question: which dependency milestones should
an autonomous implementation loop complete, and in what order, before that proof
is possible?
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)


@dataclass(frozen=True)
class IntegrationDependencyNode:
    """One delivery milestone required for an integration capability."""

    id: str
    title: str
    depends_on: tuple[str, ...]
    deliverables: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["depends_on"] = list(self.depends_on)
        data["deliverables"] = list(self.deliverables)
        return data


def _base_catalog_node() -> IntegrationDependencyNode:
    return IntegrationDependencyNode(
        id="catalog-contract",
        title="Catalog contract and rollout intent",
        depends_on=(),
        deliverables=(
            "resolved catalog entry with business case",
            "auth model and required scopes reviewed",
            "owner-visible future unlocks documented",
        ),
    )


def _auth_sandbox_node(auth_model: str) -> IntegrationDependencyNode:
    return IntegrationDependencyNode(
        id="auth-sandbox",
        title="Least-privilege authorization sandbox",
        depends_on=("catalog-contract",),
        deliverables=(
            f"{auth_model} authorization path modeled without storing live secrets",
            "required scopes mapped to read/write risk tiers",
            "token refresh, revocation, and tenant boundary documented",
        ),
    )


def _webhook_ingress_node() -> IntegrationDependencyNode:
    return IntegrationDependencyNode(
        id="webhook-ingress",
        title="Signed ingress normalization",
        depends_on=("auth-sandbox",),
        deliverables=(
            "external event ids preserved for idempotency",
            "workspace or tenant routing fields normalized",
            "unsupported events fail closed with auditable reasons",
        ),
    )


_CATEGORY_NODE: dict[CapabilityCategory, IntegrationDependencyNode] = {
    "communication": IntegrationDependencyNode(
        id="communication-loop",
        title="Human collaboration loop mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "source channel, thread, and actor mapped to Omnigent signals",
            "approval prompts carry task and escalation context",
            "outbound message rate-limit policy documented",
        ),
    ),
    "project_management": IntegrationDependencyNode(
        id="work-item-mapping",
        title="Work tracker lifecycle mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "external issue/card states mapped to Omnigent Task lifecycle",
            "comment, checklist, and assignee attribution preserved",
            "status write-back conflict policy documented",
        ),
    ),
    "knowledge": IntegrationDependencyNode(
        id="knowledge-provenance",
        title="Scoped knowledge provenance mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "selected files, pages, or databases modeled as bounded read sets",
            "write operations include source task and agent provenance",
            "broad search or mailbox access marked for approval gating",
        ),
    ),
    "developer": IntegrationDependencyNode(
        id="developer-change-safety",
        title="Review-safe engineering automation mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "repository permissions scoped to installation or project",
            "code and CI mutations routed through reviewable pull requests",
            "failed checks and review comments mapped into task evidence",
        ),
    ),
    "crm_support": IntegrationDependencyNode(
        id="customer-record-mapping",
        title="Customer record safety mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "tickets, contacts, deals, or conversations mapped to stable entities",
            "public replies identified as approval-gated writes",
            "before and after summaries required for record updates",
        ),
    ),
    "commerce_billing": IntegrationDependencyNode(
        id="revenue-risk-mapping",
        title="Revenue-side-effect risk mapping",
        depends_on=("webhook-ingress",),
        deliverables=(
            "read-only revenue context separated from payment mutations",
            "refund, cancellation, and billing writes assigned approval tiers",
            "financial anomaly signals include provider object links",
        ),
    ),
    "workflow_harness": IntegrationDependencyNode(
        id="workflow-schema",
        title="Deterministic workflow schema",
        depends_on=("catalog-contract",),
        deliverables=(
            "typed phase input/output contract",
            "stable phase and edge identifiers",
            "idempotency and retry policy declared per phase",
        ),
    ),
}

_POLICY_NODE = IntegrationDependencyNode(
    id="policy-and-idempotency",
    title="Policy gates and deterministic replay",
    depends_on=("work-item-mapping",),
    deliverables=(
        "read and write operations separated behind policy tiers",
        "idempotency keys derived from stable provider identifiers",
        "duplicate delivery and retry outcomes documented",
    ),
)

_OBSERVABILITY_NODE = IntegrationDependencyNode(
    id="operator-observability",
    title="Operator-visible rollout evidence",
    depends_on=("policy-and-idempotency",),
    deliverables=(
        "task id, provider object id, and agent id correlation defined",
        "success and failure paths produce outcome records",
        "disablement and manual recovery owner documented",
    ),
)

_HARNESS_PHASE_COMPILER_NODE = IntegrationDependencyNode(
    id="phase-compiler",
    title="Workflow phase compiler",
    depends_on=("workflow-schema",),
    deliverables=(
        "workflow phases compile into Omnigent Tasks or tool steps",
        "agent role assignment declared for every executable phase",
        "phase outputs can be consumed by downstream phases deterministically",
    ),
)

_HARNESS_VERIFICATION_NODE = IntegrationDependencyNode(
    id="verification-harness",
    title="Workflow verification harness",
    depends_on=("phase-compiler",),
    deliverables=(
        "completion evidence captured for terminal phases",
        "failed phases surface replayable operator diagnostics",
        "dry-run mode validates graph shape before execution",
    ),
)

_HARNESS_OBSERVABILITY_NODE = IntegrationDependencyNode(
    id="operator-observability",
    title="Operator-visible workflow evidence",
    depends_on=("verification-harness",),
    deliverables=(
        "phase status and evidence visible without exposing secrets",
        "workflow run id correlates all child task outcomes",
        "disablement and manual recovery owner documented",
    ),
)


def compile_integration_dependency_graph(slug: str) -> dict | None:
    """Return a JSON-ready dependency graph for delivering one capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    if capability.category == "workflow_harness":
        nodes = (
            _base_catalog_node(),
            _CATEGORY_NODE[capability.category],
            _HARNESS_PHASE_COMPILER_NODE,
            _HARNESS_VERIFICATION_NODE,
            _HARNESS_OBSERVABILITY_NODE,
        )
    else:
        category_node = _CATEGORY_NODE[capability.category]
        policy_node = IntegrationDependencyNode(
            id=_POLICY_NODE.id,
            title=_POLICY_NODE.title,
            depends_on=(category_node.id,),
            deliverables=_POLICY_NODE.deliverables,
        )
        nodes = (
            _base_catalog_node(),
            _auth_sandbox_node(capability.auth_model),
            _webhook_ingress_node(),
            category_node,
            policy_node,
            _OBSERVABILITY_NODE,
        )

    return {
        "object": "integration_dependency_graph",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "recommended_sequence": [node.id for node in nodes],
        "nodes": [node.to_dict() for node in nodes],
    }
