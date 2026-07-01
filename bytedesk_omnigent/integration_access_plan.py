"""Deterministic access-control plans for integration capability rollout.

The integration catalog describes what a connector should do. This module turns a
catalog entry into a least-privilege access plan ByteDesk Platform can surface
before enabling an integration, without reading tenant secrets or provider data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)

IntegrationRiskTier = Literal["internal_harness", "external_read", "external_write"]


@dataclass(frozen=True)
class AccessRole:
    """One least-privilege role for operating an integration capability."""

    id: str
    title: str
    allowed_actions: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["allowed_actions"] = list(self.allowed_actions)
        return data


_VIEWER_ROLE = AccessRole(
    id="integration_viewer",
    title="Integration viewer",
    allowed_actions=("read_catalog", "read_provider_objects", "read_execution_evidence"),
)
_OPERATOR_ROLE = AccessRole(
    id="integration_operator",
    title="Integration operator",
    allowed_actions=("trigger_read_only_sync", "draft_provider_write", "request_write_approval"),
)
_APPROVER_ROLE = AccessRole(
    id="integration_approver",
    title="Integration approver",
    allowed_actions=("approve_provider_write", "disable_connector"),
)
_WORKFLOW_DESIGNER_ROLE = AccessRole(
    id="workflow_designer",
    title="Workflow designer",
    allowed_actions=("read_catalog", "draft_workflow_template", "run_dry_run"),
)
_WORKFLOW_PUBLISHER_ROLE = AccessRole(
    id="workflow_publisher",
    title="Workflow publisher",
    allowed_actions=("publish_workflow_template", "activate_cross_agent_workflow"),
)

_CATEGORY_APPROVALS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "provider-side writes",
        "message or comment publication",
        "connector disablement",
    ),
    "project_management": (
        "provider-side writes",
        "status transition write-back",
        "connector disablement",
    ),
    "knowledge": (
        "provider-side writes",
        "broad document or mailbox search",
        "connector disablement",
    ),
    "developer": (
        "provider-side writes",
        "repository or CI mutation",
        "connector disablement",
    ),
    "crm_support": (
        "provider-side writes",
        "public customer reply publication",
        "connector disablement",
    ),
    "commerce_billing": (
        "provider-side writes",
        "revenue-affecting mutation",
        "connector disablement",
    ),
    "workflow_harness": (
        "template publication",
        "cross-agent workflow activation",
    ),
}


_READ_SCOPE_MARKERS = ("read", "history", "users:read", "contents:read", "checks:read")
_WRITE_SCOPE_MARKERS = ("write", "chat:write", "pull_requests:write", "issues:write")
_OFFLINE_SCOPE_MARKERS = ("offline", "refresh")


def compile_integration_access_plan(slug: str) -> dict | None:
    """Return a JSON-ready least-privilege access plan for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    return {
        "object": "integration_access_plan",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "least_privilege_roles": [role.to_dict() for role in _roles_for_risk(risk_tier)],
        "approval_required_for": list(_CATEGORY_APPROVALS[capability.category]),
        "blocked_without_approval": list(_blocked_actions_for_risk(risk_tier)),
        "scope_review": _scope_review(capability.required_scopes),
    }


def _roles_for_risk(risk_tier: IntegrationRiskTier) -> tuple[AccessRole, ...]:
    if risk_tier == "internal_harness":
        return (_WORKFLOW_DESIGNER_ROLE, _WORKFLOW_PUBLISHER_ROLE)
    if risk_tier == "external_read":
        return (_VIEWER_ROLE, _OPERATOR_ROLE)
    return (_VIEWER_ROLE, _OPERATOR_ROLE, _APPROVER_ROLE)


def _blocked_actions_for_risk(risk_tier: IntegrationRiskTier) -> tuple[str, ...]:
    if risk_tier == "internal_harness":
        return (
            "publish reusable template",
            "activate workflow across agents",
        )
    if risk_tier == "external_read":
        return (
            "write external object",
            "publish outbound communication",
        )
    return (
        "write external object",
        "publish outbound communication",
        "delete or revoke provider resource",
    )


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any(_is_write_scope(scope) for scope in required_scopes):
        return "external_write"
    return "external_read"


def _scope_review(required_scopes: tuple[str, ...]) -> dict[str, list[str]]:
    return {
        "read_scopes": [
            scope
            for scope in required_scopes
            if _contains_marker(scope, _READ_SCOPE_MARKERS)
        ],
        "write_scopes": [scope for scope in required_scopes if _is_write_scope(scope)],
        "offline_scopes": [
            scope
            for scope in required_scopes
            if _contains_marker(scope, _OFFLINE_SCOPE_MARKERS)
        ],
    }


def _is_write_scope(scope: str) -> bool:
    return _contains_marker(scope, _WRITE_SCOPE_MARKERS)


def _contains_marker(scope: str, markers: tuple[str, ...]) -> bool:
    normalized = scope.lower()
    return any(marker in normalized for marker in markers)
