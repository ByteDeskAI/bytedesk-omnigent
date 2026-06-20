"""Deterministic connected-app approval plans for third-party integrations.

The compiler is intentionally pure: ByteDesk Platform can preview exactly which
human gates an OAuth/service installation needs before Omnigent receives tokens
or executes writeback against a third-party system.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum


class ApprovalLevel(str, Enum):
    """Human approval required before an integration can be activated."""

    NONE = "none"
    USER = "user"
    ADMIN = "admin"
    TWO_KEY = "two_key"


class RiskLevel(str, Enum):
    """Coarse integration risk classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class IntegrationApprovalPlan:
    """Deterministic approval contract returned to Platform setup flows."""

    provider: str
    normalized_scopes: list[str]
    requested_operations: list[str]
    risk_level: str
    required_approval: str
    gates: list[str]
    readonly_scopes: list[str]
    write_scopes: list[str]
    admin_scopes: list[str]
    reasons: list[str]
    idempotency_key: str
    recommended_token_owner: str
    byte_desk_mount_hint: str

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return asdict(self)


_ADMIN_MARKERS = (
    "admin",
    "manage",
    "settings",
    "users",
    "members",
    "workspace",
    "organization",
    "org",
    "security",
    "billing",
    "offline_access",
    "refresh_token",
    "crm.objects.owners.write",
)

_WRITE_MARKERS = (
    "write",
    "create",
    "update",
    "delete",
    "modify",
    "send",
    "post",
    "chat:write",
    "commands",
    "issues:write",
    "pull_requests:write",
    "repo",
    "contents:write",
    "tasks:write",
    "contacts.write",
    "tickets.write",
    "payment_intents",
    "charges",
    "refunds",
)

_READONLY_MARKERS = ("read", "readonly", "metadata", "history", "search", "profile")

_SYSTEM_OF_RECORD_PROVIDERS = {
    "github",
    "google_workspace",
    "hubspot",
    "salesforce",
    "stripe",
    "shopify",
    "zendesk",
    "intercom",
    "jira",
    "linear",
    "notion",
}

_PROVIDER_ALIASES = {
    "google": "google_workspace",
    "google-workspace": "google_workspace",
    "gworkspace": "google_workspace",
    "microsoft-teams": "microsoft_teams",
    "ms-teams": "microsoft_teams",
    "github-app": "github",
}

_OPERATION_WRITE_MARKERS = (
    "write",
    "writeback",
    "create",
    "update",
    "delete",
    "send",
    "post",
    "refund",
    "charge",
    "sync_back",
)


def compile_integration_approval_plan(
    *,
    provider: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    requested_operations: list[str] | tuple[str, ...] | None = None,
    writeback_enabled: bool = False,
) -> IntegrationApprovalPlan:
    """Compile a deterministic approval plan for a connected-app install.

    :param provider: Third-party service slug, e.g. ``github`` or ``slack``.
    :param scopes: OAuth/app scopes requested by the installation.
    :param requested_operations: Planned autonomous actions using the token.
    :param writeback_enabled: Whether Omnigent may write back to the provider.
    :returns: A stable approval/risk plan suitable for preview and audit logs.
    :raises ValueError: if ``provider`` is blank.
    """
    normalized_provider = _normalize_provider(provider)
    normalized_scopes = _normalize_items(scopes or [])
    operations = _normalize_items(requested_operations or [])

    readonly_scopes = [s for s in normalized_scopes if _is_readonly_scope(s)]
    admin_scopes = [
        s
        for s in normalized_scopes
        if _contains_marker(s, _ADMIN_MARKERS) and s not in readonly_scopes
    ]
    write_scopes = [
        s
        for s in normalized_scopes
        if _contains_marker(s, _WRITE_MARKERS) and s not in readonly_scopes
    ]
    write_operations = [op for op in operations if _contains_marker(op, _OPERATION_WRITE_MARKERS)]

    reasons: list[str] = []
    if not normalized_scopes:
        reasons.append("no_scopes_declared")
    if readonly_scopes and not write_scopes and not admin_scopes and not writeback_enabled:
        reasons.append("readonly_scopes_only")
    if write_scopes:
        reasons.append("third_party_write_scopes")
    if admin_scopes:
        reasons.append("admin_or_workspace_scopes")
    if writeback_enabled or write_operations:
        reasons.append("autonomous_writeback_requested")
    if normalized_provider in _SYSTEM_OF_RECORD_PROVIDERS:
        reasons.append("system_of_record_provider")

    risk_level, approval = _risk_and_approval(
        provider=normalized_provider,
        write_scopes=write_scopes,
        admin_scopes=admin_scopes,
        writeback_enabled=writeback_enabled,
        write_operations=write_operations,
    )
    gates = _gates_for(approval, writeback_enabled=writeback_enabled, write_scopes=write_scopes)

    return IntegrationApprovalPlan(
        provider=normalized_provider,
        normalized_scopes=normalized_scopes,
        requested_operations=operations,
        risk_level=risk_level.value,
        required_approval=approval.value,
        gates=gates,
        readonly_scopes=readonly_scopes,
        write_scopes=write_scopes,
        admin_scopes=admin_scopes,
        reasons=reasons,
        idempotency_key=_idempotency_key(normalized_provider, normalized_scopes, operations),
        recommended_token_owner=("workspace_admin" if admin_scopes else "installing_user"),
        byte_desk_mount_hint=f"/integrations/{normalized_provider}/approval-preview",
    )


def _risk_and_approval(
    *,
    provider: str,
    write_scopes: list[str],
    admin_scopes: list[str],
    writeback_enabled: bool,
    write_operations: list[str],
) -> tuple[RiskLevel, ApprovalLevel]:
    if admin_scopes and (
        writeback_enabled or write_scopes or provider in {"stripe", "salesforce"}
    ):
        return RiskLevel.CRITICAL, ApprovalLevel.TWO_KEY
    if admin_scopes:
        return RiskLevel.HIGH, ApprovalLevel.ADMIN
    if writeback_enabled or write_operations:
        if provider in _SYSTEM_OF_RECORD_PROVIDERS or write_scopes:
            return RiskLevel.HIGH, ApprovalLevel.ADMIN
        return RiskLevel.MEDIUM, ApprovalLevel.USER
    if write_scopes:
        return RiskLevel.MEDIUM, ApprovalLevel.USER
    return RiskLevel.LOW, ApprovalLevel.NONE


def _gates_for(
    approval: ApprovalLevel, *, writeback_enabled: bool, write_scopes: list[str]
) -> list[str]:
    gates: list[str] = ["scope_preview"]
    if approval in {ApprovalLevel.USER, ApprovalLevel.ADMIN, ApprovalLevel.TWO_KEY}:
        gates.append("installer_consent")
    if approval in {ApprovalLevel.ADMIN, ApprovalLevel.TWO_KEY}:
        gates.append("workspace_admin_approval")
    if approval is ApprovalLevel.TWO_KEY:
        gates.append("second_reviewer_approval")
    if writeback_enabled or write_scopes:
        gates.append("dry_run_before_writeback")
    gates.append("audit_log_entry")
    return gates


def _normalize_provider(provider: str) -> str:
    normalized = _slug(provider)
    if not normalized:
        raise ValueError("provider is required")
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _normalize_items(items: list[str] | tuple[str, ...]) -> list[str]:
    return sorted({_slug(item) for item in items if _slug(item)})


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.:-]+", "_", value.strip().lower()).strip("_")


def _contains_marker(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _is_readonly_scope(scope: str) -> bool:
    has_read = _contains_marker(scope, _READONLY_MARKERS)
    has_write = _contains_marker(scope, _WRITE_MARKERS)
    return has_read and not has_write


def _idempotency_key(provider: str, scopes: list[str], operations: list[str]) -> str:
    scope_part = ",".join(scopes) if scopes else "no-scopes"
    op_part = ",".join(operations) if operations else "no-ops"
    return f"integration-approval:{provider}:{scope_part}:{op_part}"
