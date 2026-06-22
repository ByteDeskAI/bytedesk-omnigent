"""Deterministic invocation contracts for connected-app capability calls.

The integration catalog says what Omnigent should support. This module turns one
catalog capability into a small, JSON-ready contract a connected application can
use before invoking Omnigent: what context must be supplied, which execution mode
and approval posture apply, how activity should be projected, and where rollout
verification evidence lives.
"""

from __future__ import annotations

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)

_CORE_CONTEXT_REFS = ("tenant", "requester", "goal")

_CATEGORY_CONTEXT_REFS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": ("source_event",),
    "project_management": ("source_event",),
    "knowledge": ("source_event",),
    "developer": ("source_event", "repository"),
    "crm_support": ("source_event", "customer_record"),
    "commerce_billing": ("source_event", "commerce_account"),
    "workflow_harness": ("workflow_blueprint", "phase_graph"),
}

_CATEGORY_ROUTING_HINTS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "normalize provider event into an Omnigent signal",
        "project status updates back into the source conversation",
        "route mutation requests through approval cards before provider writes",
    ),
    "project_management": (
        "normalize provider event into an Omnigent signal",
        "bind external work item ids to Omnigent task ids",
        "preserve source-of-truth status ownership on write-back",
    ),
    "knowledge": (
        "normalize provider event into an Omnigent signal",
        "scope reads to selected files, pages, or databases",
        "record provenance for every generated write or append",
    ),
    "developer": (
        "normalize provider event into an Omnigent signal",
        "prefer reviewable pull requests for code mutations",
        "attach CI and review evidence to the task outcome",
    ),
    "crm_support": (
        "normalize provider event into an Omnigent signal",
        "capture before and after summaries for record updates",
        "require approval before public customer-facing responses",
    ),
    "commerce_billing": (
        "normalize provider event into an Omnigent signal",
        "separate read-only revenue context from payment mutations",
        "require explicit approval for refunds, cancellations, and billing writes",
    ),
    "workflow_harness": (
        "compile deterministic phase graph before agent dispatch",
        "bind every terminal phase to completion evidence",
        "prefer tool nodes for deterministic steps and agent nodes for judgment",
    ),
}


def compile_integration_invocation_contract(
    slug: str,
    *,
    requester: str,
    context_refs: tuple[str, ...] = (),
    idempotency_key: str,
) -> dict | None:
    """Return a JSON-ready connected-app invocation contract for a capability.

    The compiler is pure and secret-free: callers supply opaque context ref
    strings, never raw credentials or provider payloads. A connected app can use
    the contract to decide whether it has enough context to call Omnigent and
    which approval/projection surfaces to prepare.
    """

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    required_context_refs = (*_CORE_CONTEXT_REFS, *_CATEGORY_CONTEXT_REFS[capability.category])
    missing_context_refs = _missing_context_refs(
        capability.category,
        supplied=context_refs,
        required=required_context_refs,
    )

    return {
        "object": "integration_invocation_contract",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "requester": requester,
        "idempotency_key": idempotency_key,
        "execution_mode": _execution_mode(capability.category),
        "risk_tier": risk_tier,
        "approval_mode": _approval_mode(risk_tier),
        "required_context_refs": list(required_context_refs),
        "provided_context_refs": list(context_refs),
        "missing_context_refs": list(missing_context_refs),
        "routing_hints": list(_CATEGORY_ROUTING_HINTS[capability.category]),
        "activity_projection": {
            "status_channel": f"{capability.slug}.status",
            "event_stream": f"integration.{capability.slug}",
            "safe_for_operator_ui": True,
        },
        "verification_matrix_path": (
            f"/v1/integration-capabilities/{capability.slug}/verification-matrix"
        ),
    }


def _risk_tier(category: CapabilityCategory, required_scopes: tuple[str, ...]) -> str:
    if category == "workflow_harness":
        return "internal_harness"
    mutating_terms = ("write", "update", "insert", "send", "delete", "refund", "cancel")
    if any(term in scope.lower() for scope in required_scopes for term in mutating_terms):
        return "external_write"
    if required_scopes and not all(_is_read_only_scope(scope) for scope in required_scopes):
        return "external_write"
    return "external_read"


def _is_read_only_scope(scope: str) -> bool:
    normalized = scope.lower()
    return (
        normalized == "read_only"
        or normalized == "read"
        or normalized.endswith((":read", ".read", ":history"))
    )


def _approval_mode(risk_tier: str) -> str:
    if risk_tier == "internal_harness":
        return "operator_review"
    if risk_tier == "external_write":
        return "approval_required_for_mutations"
    return "read_only_preapproved"


def _execution_mode(category: CapabilityCategory) -> str:
    if category == "workflow_harness":
        return "workflow_harness"
    return "connected_app"


def _missing_context_refs(
    category: CapabilityCategory,
    *,
    supplied: tuple[str, ...],
    required: tuple[str, ...],
) -> tuple[str, ...]:
    supplied_exact = set(supplied)
    missing = [ref for ref in required if ref not in supplied_exact]

    # Workflow-blueprint and phase-graph refs may be derived from opaque app refs
    # (for example an Office workflow template id) before agent dispatch. Keep the
    # contract focused on caller-supplied primitives for harness invocations.
    if category == "workflow_harness":
        missing = [ref for ref in missing if ref in _CORE_CONTEXT_REFS]

    return tuple(missing)
