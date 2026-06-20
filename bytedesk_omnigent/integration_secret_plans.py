"""Deterministic connected-app secret readiness plans for Omnigent integrations.

The compiler is intentionally pure: ByteDesk Platform, Office, or an autonomous
implementation loop can ask what credentials, webhook secrets, scopes, and
verification probes a third-party integration needs before it attempts to install
or wake agents from external systems.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RequiredSecret:
    """One credential or secret that must be provisioned for a provider."""

    env_var: str
    purpose: str
    required: bool = True


@dataclass(frozen=True)
class ProvisioningStep:
    """One deterministic setup step shown to Platform/operator tooling."""

    id: str
    title: str
    detail: str


@dataclass(frozen=True)
class IntegrationSecretPlan:
    """Credential and verification plan for a connected-app provider."""

    provider: str
    ingress_source: str
    workspace_id: str
    required_secrets: list[RequiredSecret]
    oauth_scopes: list[str]
    recommended_match_keys: list[str]
    approval_gates: list[str]
    provisioning_steps: list[ProvisioningStep]
    verification: dict[str, str]
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)


@dataclass(frozen=True)
class _ProviderBlueprint:
    provider: str
    ingress_source: str
    secret_env_prefix: str
    secrets: tuple[tuple[str, str], ...]
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    match_keys: tuple[str, ...]
    aliases: tuple[str, ...] = ()


_BLUEPRINTS: tuple[_ProviderBlueprint, ...] = (
    _ProviderBlueprint(
        provider="hubspot",
        ingress_source="hubspot",
        secret_env_prefix="OMNIGENT_HUBSPOT_",
        secrets=(
            ("CLIENT_ID", "OAuth app client id"),
            ("CLIENT_SECRET", "OAuth app client secret"),
            ("WEBHOOK_SECRET", "HubSpot webhook signature secret"),
        ),
        read_scopes=("crm.objects.contacts.read", "crm.objects.deals.read"),
        write_scopes=("crm.objects.contacts.write", "crm.objects.deals.write"),
        match_keys=("contact.creation", "deal.propertyChange", "ticket.creation"),
    ),
    _ProviderBlueprint(
        provider="salesforce",
        ingress_source="salesforce",
        secret_env_prefix="OMNIGENT_SALESFORCE_",
        secrets=(
            ("CLIENT_ID", "Connected app client id"),
            ("CLIENT_SECRET", "Connected app client secret"),
            ("WEBHOOK_SECRET", "Platform event or outbound-message signing secret"),
        ),
        read_scopes=("api", "refresh_token"),
        write_scopes=("full",),
        match_keys=("LeadChangeEvent", "CaseChangeEvent", "OpportunityChangeEvent"),
    ),
    _ProviderBlueprint(
        provider="zendesk",
        ingress_source="zendesk",
        secret_env_prefix="OMNIGENT_ZENDESK_",
        secrets=(
            ("SUBDOMAIN", "Zendesk account subdomain"),
            ("CLIENT_ID", "OAuth client id"),
            ("CLIENT_SECRET", "OAuth client secret"),
            ("WEBHOOK_SECRET", "Webhook signing secret"),
        ),
        read_scopes=("read",),
        write_scopes=("write",),
        match_keys=("ticket.created", "ticket.updated", "comment.created"),
    ),
    _ProviderBlueprint(
        provider="intercom",
        ingress_source="intercom",
        secret_env_prefix="OMNIGENT_INTERCOM_",
        secrets=(
            ("CLIENT_ID", "OAuth client id"),
            ("CLIENT_SECRET", "OAuth client secret"),
            ("WEBHOOK_SECRET", "Webhook topic signing secret"),
        ),
        read_scopes=("read_conversations", "read_contacts"),
        write_scopes=("write_conversations",),
        match_keys=("conversation.user.created", "conversation.admin.replied"),
    ),
    _ProviderBlueprint(
        provider="google_workspace",
        ingress_source="google-workspace",
        secret_env_prefix="OMNIGENT_GOOGLE_WORKSPACE_",
        secrets=(
            ("CLIENT_ID", "Google OAuth client id"),
            ("CLIENT_SECRET", "Google OAuth client secret"),
            ("PUBSUB_VERIFICATION_TOKEN", "Push notification verification token"),
        ),
        read_scopes=(
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
        write_scopes=(
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.send",
        ),
        match_keys=("calendar.event.changed", "gmail.message.received"),
        aliases=("google-workspace", "google", "gworkspace"),
    ),
    _ProviderBlueprint(
        provider="airtable",
        ingress_source="airtable",
        secret_env_prefix="OMNIGENT_AIRTABLE_",
        secrets=(
            ("CLIENT_ID", "OAuth client id"),
            ("CLIENT_SECRET", "OAuth client secret"),
            ("WEBHOOK_SECRET", "Webhook MAC secret"),
        ),
        read_scopes=("data.records:read", "schema.bases:read"),
        write_scopes=("data.records:write",),
        match_keys=("table.records.changed", "base.schema.changed"),
    ),
    _ProviderBlueprint(
        provider="discord",
        ingress_source="discord",
        secret_env_prefix="OMNIGENT_DISCORD_",
        secrets=(
            ("APPLICATION_ID", "Discord application id"),
            ("PUBLIC_KEY", "Interactions signature public key"),
            ("BOT_TOKEN", "Bot token for writeback"),
        ),
        read_scopes=("applications.commands",),
        write_scopes=("bot",),
        match_keys=("interaction.command", "message.create"),
    ),
)


def _normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("-", "_").replace(" ", "_")


def _resolve_blueprint(provider: str) -> _ProviderBlueprint:
    normalized = _normalize_provider(provider)
    for blueprint in _BLUEPRINTS:
        aliases = {_normalize_provider(alias) for alias in blueprint.aliases}
        if normalized == blueprint.provider or normalized in aliases:
            return blueprint
    supported = ", ".join(sorted(blueprint.provider for blueprint in _BLUEPRINTS))
    raise ValueError(f"unsupported provider {provider!r}; supported providers: {supported}")


def compile_integration_secret_plan(payload: dict[str, Any]) -> IntegrationSecretPlan:
    """Compile a deterministic credential readiness plan for one provider.

    Required input: ``provider`` and ``workspace_id``. Optional
    ``requested_events`` narrows the suggested match keys; ``writeback`` controls
    whether write scopes and the human approval gate are included.
    """

    provider = str(payload.get("provider") or "").strip()
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    if not workspace_id:
        raise ValueError("workspace_id is required")

    blueprint = _resolve_blueprint(provider)
    writeback = bool(payload.get("writeback"))
    requested_events = [str(event) for event in payload.get("requested_events") or []]
    match_keys = requested_events or list(blueprint.match_keys)
    scopes = list(blueprint.read_scopes)
    if writeback:
        scopes.extend(scope for scope in blueprint.write_scopes if scope not in scopes)

    required_secrets = [
        RequiredSecret(env_var=f"{blueprint.secret_env_prefix}{suffix}", purpose=purpose)
        for suffix, purpose in blueprint.secrets
    ]
    approval_gates = ["install_connected_app"]
    if writeback:
        approval_gates.append("approve_autonomous_writeback")

    return IntegrationSecretPlan(
        provider=blueprint.provider,
        ingress_source=blueprint.ingress_source,
        workspace_id=workspace_id,
        required_secrets=required_secrets,
        oauth_scopes=scopes,
        recommended_match_keys=match_keys,
        approval_gates=approval_gates,
        provisioning_steps=[
            ProvisioningStep(
                id="collect-oauth-app",
                title="Collect connected-app credentials",
                detail="Store provider OAuth/app credentials under the returned env vars.",
            ),
            ProvisioningStep(
                id="configure-webhook-secret",
                title="Configure webhook signing secret",
                detail="Use the provider webhook secret as the Omnigent ingress secret.",
            ),
            ProvisioningStep(
                id="bind-events-to-signals",
                title="Bind external events to parked Omnigent signals",
                detail=(
                    "Create webhook bindings for the recommended match keys before "
                    "enabling delivery."
                ),
            ),
            ProvisioningStep(
                id="verify-before-activation",
                title="Verify readiness before activation",
                detail="Confirm all required secrets and scopes before accepting provider events.",
            ),
        ],
        verification={
            "secret_env_prefix": blueprint.secret_env_prefix,
            "ingress_url_template": f"/v1/ingress/{blueprint.ingress_source}",
            "dry_run_probe": f"/v1/integration-secret-plans/compile:{blueprint.provider}",
        },
        idempotency_key=f"integration-secret-plan:{blueprint.provider}:{workspace_id}",
    )
