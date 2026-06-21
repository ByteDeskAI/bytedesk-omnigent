"""Deterministic OAuth scope review for connected-app integrations.

Before Omnigent or ByteDesk Platform installs a third-party connected app, this
module classifies the requested OAuth scopes against a small built-in catalog and
returns an approval posture. It is pure and secret-free: no provider APIs are
called and no credentials are read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict


class IntegrationScopeRisk(str, Enum):
    """Coarse approval posture for a connected-app scope request."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


ScopeRisk = Literal["low", "medium", "high"]


class PolicyRecommendation(TypedDict):
    """Governance policy Omnigent should attach before enabling the app."""

    policy: str
    reason: str


@dataclass(frozen=True)
class ScopeDefinition:
    """Known scope metadata for one integration provider."""

    scope: str
    risk: IntegrationScopeRisk
    reason: str


@dataclass(frozen=True)
class IntegrationScopeReview:
    """A JSON-ready review result for a requested OAuth scope set."""

    service: str
    requested_scopes: tuple[str, ...]
    approved_scopes: tuple[str, ...]
    high_risk_scopes: tuple[str, ...]
    unknown_scopes: tuple[str, ...]
    risk: IntegrationScopeRisk
    requires_human_approval: bool
    recommendations: tuple[str, ...]
    policy_recommendations: tuple[PolicyRecommendation, ...]


_SCOPE_CATALOG: dict[str, tuple[ScopeDefinition, ...]] = {
    "slack": (
        ScopeDefinition(
            "channels:history", IntegrationScopeRisk.LOW, "read public channel history"
        ),
        ScopeDefinition("channels:read", IntegrationScopeRisk.LOW, "list public channels"),
        ScopeDefinition("chat:write", IntegrationScopeRisk.LOW, "post messages as the app"),
        ScopeDefinition(
            "users:read", IntegrationScopeRisk.LOW, "read workspace user profile basics"
        ),
        ScopeDefinition("files:write", IntegrationScopeRisk.MEDIUM, "upload files into Slack"),
        ScopeDefinition(
            "admin.*", IntegrationScopeRisk.HIGH, "admin-wide Slack workspace control"
        ),
    ),
    "github": (
        ScopeDefinition("read:user", IntegrationScopeRisk.LOW, "read account identity"),
        ScopeDefinition("repo:status", IntegrationScopeRisk.LOW, "read and write commit statuses"),
        ScopeDefinition("issues:read", IntegrationScopeRisk.LOW, "read issue metadata"),
        ScopeDefinition("issues:write", IntegrationScopeRisk.MEDIUM, "create and update issues"),
        ScopeDefinition(
            "pull_requests:write", IntegrationScopeRisk.MEDIUM, "create and update pull requests"
        ),
        ScopeDefinition("repo", IntegrationScopeRisk.HIGH, "full repository read/write access"),
        ScopeDefinition("admin:org", IntegrationScopeRisk.HIGH, "organization administration"),
    ),
    "linear": (
        ScopeDefinition("read", IntegrationScopeRisk.LOW, "read Linear workspace objects"),
        ScopeDefinition("write", IntegrationScopeRisk.MEDIUM, "create and update Linear objects"),
        ScopeDefinition("admin", IntegrationScopeRisk.HIGH, "Linear workspace administration"),
    ),
    "jira": (
        ScopeDefinition("read:jira-work", IntegrationScopeRisk.LOW, "read Jira work items"),
        ScopeDefinition(
            "write:jira-work", IntegrationScopeRisk.MEDIUM, "create and update Jira work items"
        ),
        ScopeDefinition(
            "manage:jira-configuration", IntegrationScopeRisk.HIGH, "modify Jira configuration"
        ),
    ),
    "notion": (
        ScopeDefinition("read_content", IntegrationScopeRisk.LOW, "read shared Notion pages"),
        ScopeDefinition("insert_content", IntegrationScopeRisk.MEDIUM, "create page content"),
        ScopeDefinition("update_content", IntegrationScopeRisk.MEDIUM, "update page content"),
    ),
    "google-workspace": (
        ScopeDefinition(
            "https://www.googleapis.com/auth/gmail.readonly",
            IntegrationScopeRisk.LOW,
            "read Gmail messages",
        ),
        ScopeDefinition(
            "https://www.googleapis.com/auth/calendar.readonly",
            IntegrationScopeRisk.LOW,
            "read calendars",
        ),
        ScopeDefinition(
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            IntegrationScopeRisk.LOW,
            "read Drive metadata",
        ),
        ScopeDefinition(
            "https://www.googleapis.com/auth/gmail.send",
            IntegrationScopeRisk.HIGH,
            "send email as the user",
        ),
        ScopeDefinition(
            "https://www.googleapis.com/auth/drive",
            IntegrationScopeRisk.HIGH,
            "full Drive file access",
        ),
        ScopeDefinition(
            "https://www.googleapis.com/auth/admin.directory.user",
            IntegrationScopeRisk.HIGH,
            "admin directory user management",
        ),
    ),
    "hubspot": (
        ScopeDefinition(
            "crm.objects.contacts.read", IntegrationScopeRisk.LOW, "read CRM contacts"
        ),
        ScopeDefinition(
            "crm.objects.contacts.write", IntegrationScopeRisk.MEDIUM, "write CRM contacts"
        ),
        ScopeDefinition("automation", IntegrationScopeRisk.HIGH, "change HubSpot automation"),
    ),
    "salesforce": (
        ScopeDefinition("api", IntegrationScopeRisk.MEDIUM, "Salesforce API access"),
        ScopeDefinition(
            "refresh_token", IntegrationScopeRisk.MEDIUM, "offline access via refresh tokens"
        ),
        ScopeDefinition("full", IntegrationScopeRisk.HIGH, "full Salesforce account access"),
    ),
    "zendesk": (
        ScopeDefinition("read", IntegrationScopeRisk.LOW, "read support tickets"),
        ScopeDefinition("write", IntegrationScopeRisk.MEDIUM, "create and update support tickets"),
        ScopeDefinition("impersonate", IntegrationScopeRisk.HIGH, "act as another Zendesk user"),
    ),
}


def review_integration_scopes(
    *, service: str, requested_scopes: Iterable[str]
) -> IntegrationScopeReview:
    """Review requested OAuth scopes for a connected-app install.

    :param service: Provider slug, for example ``slack`` or ``google-workspace``.
    :param requested_scopes: Scopes requested by the connected-app manifest.
    :returns: Approval posture and governance recommendations.
    """
    normalized_service = service.strip().lower()
    requested = _dedupe_scope_order(requested_scopes)
    known = {
        definition.scope: definition for definition in _SCOPE_CATALOG.get(normalized_service, ())
    }

    approved: list[str] = []
    high_risk: list[str] = []
    unknown: list[str] = []
    recommendations: list[str] = []
    risk = IntegrationScopeRisk.LOW

    if normalized_service not in _SCOPE_CATALOG:
        unknown = list(requested)
        risk = IntegrationScopeRisk.HIGH
        recommendations.append(
            f"Service {normalized_service!r} is not in the built-in catalog; "
            "require manual review before OAuth install."
        )
    else:
        for scope in requested:
            definition = known.get(scope) or _match_wildcard_scope(scope, known.values())
            if definition is None:
                unknown.append(scope)
                risk = IntegrationScopeRisk.HIGH
                recommendations.append(
                    f"Scope {scope!r} is unknown for {normalized_service}; "
                    "treat as high risk until cataloged."
                )
                continue
            approved.append(scope)
            risk = _max_risk(risk, definition.risk)
            if definition.risk is IntegrationScopeRisk.HIGH:
                high_risk.append(scope)
            if definition.risk is not IntegrationScopeRisk.LOW:
                recommendations.append(f"{scope}: {definition.reason}")

    policies = _policy_recommendations(risk, high_risk=tuple(high_risk), unknown=tuple(unknown))
    return IntegrationScopeReview(
        service=normalized_service,
        requested_scopes=requested,
        approved_scopes=tuple(approved),
        high_risk_scopes=tuple(high_risk),
        unknown_scopes=tuple(unknown),
        risk=risk,
        requires_human_approval=risk is not IntegrationScopeRisk.LOW,
        recommendations=tuple(recommendations),
        policy_recommendations=policies,
    )


def supported_integration_services() -> tuple[str, ...]:
    """Return the service slugs supported by the built-in scope catalog."""
    return tuple(sorted(_SCOPE_CATALOG))


def _dedupe_scope_order(scopes: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in scopes:
        scope = str(raw).strip()
        if not scope or scope in seen:
            continue
        seen.add(scope)
        ordered.append(scope)
    return tuple(ordered)


def _match_wildcard_scope(
    scope: str, definitions: Iterable[ScopeDefinition]
) -> ScopeDefinition | None:
    for definition in definitions:
        if definition.scope.endswith("*") and scope.startswith(definition.scope[:-1]):
            return definition
    return None


def _max_risk(
    current: IntegrationScopeRisk, candidate: IntegrationScopeRisk
) -> IntegrationScopeRisk:
    rank = {
        IntegrationScopeRisk.LOW: 0,
        IntegrationScopeRisk.MEDIUM: 1,
        IntegrationScopeRisk.HIGH: 2,
    }
    return candidate if rank[candidate] > rank[current] else current


def _policy_recommendations(
    risk: IntegrationScopeRisk, *, high_risk: tuple[str, ...], unknown: tuple[str, ...]
) -> tuple[PolicyRecommendation, ...]:
    if risk is IntegrationScopeRisk.LOW:
        return ()
    policies: list[PolicyRecommendation] = [
        {
            "policy": "two_key_approval",
            "reason": "Require a human owner and reviewer before installing "
            "elevated OAuth scopes.",
        },
        {
            "policy": "dry_run_write_actions",
            "reason": "Start the connected app in dry-run mode until first "
            "workflow verification passes.",
        },
    ]
    if high_risk or unknown:
        policies.append(
            {
                "policy": "least_privilege_scope_trim",
                "reason": "Remove high-risk or unknown scopes unless the workflow "
                "explicitly needs them.",
            }
        )
    return tuple(policies)
