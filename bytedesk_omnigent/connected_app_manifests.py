"""Connected-app installation manifest compiler for Omnigent integrations.

This is the deterministic setup contract between ByteDesk Platform / Office and
third-party connected apps: given a provider and desired Omnigent capabilities,
compile the OAuth scopes, webhook subscriptions, ingress target, task defaults,
and approval gates needed to mount that app into autonomous agent workflows.

The compiler is pure and intentionally never stores or returns secrets. Runtime
secret material remains in the existing secret backends / ingress secret resolver.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProviderTemplate:
    """Static integration setup template for one third-party provider."""

    provider: str
    display_name: str
    auth_model: str
    base_scopes: tuple[str, ...]
    writeback_scopes: tuple[str, ...]
    webhook_events: tuple[str, ...]
    default_capability: str
    approval_gate: str
    setup_notes: tuple[str, ...]


@dataclass(frozen=True)
class ConnectedAppManifest:
    """Deterministic install manifest for one provider/workspace pair."""

    manifest_id: str
    provider: str
    display_name: str
    workspace_id: str
    tenant_id: str | None
    auth_model: str
    required_scopes: list[str]
    webhook_events: list[str]
    redirect_uri: str
    ingress_path: str
    ingress_source: str
    secret_env_var: str
    task_defaults: dict[str, Any]
    approval_gates: list[dict[str, str]]
    writeback_enabled: bool
    idempotency_key_template: str
    bytedesk_mount: dict[str, Any]
    setup_notes: list[str]


_PROVIDER_TEMPLATES: dict[str, ProviderTemplate] = {
    "slack": ProviderTemplate(
        provider="slack",
        display_name="Slack",
        auth_model="oauth2",
        base_scopes=("channels:history", "channels:read", "chat:write", "commands"),
        writeback_scopes=("reactions:write",),
        webhook_events=("app_mention", "message.channels", "slash_command"),
        default_capability="team_chat.agent_request",
        approval_gate="approval.required_before_public_channel_write",
        setup_notes=(
            "Subscribe Slack Events API to the compiled ingress URL.",
            "Use Slack request signing secret as the ingress secret value.",
        ),
    ),
    "github": ProviderTemplate(
        provider="github",
        display_name="GitHub",
        auth_model="github_app",
        base_scopes=("contents:read", "issues:read", "pull_requests:read"),
        writeback_scopes=("issues:write", "pull_requests:write"),
        webhook_events=("issues", "pull_request", "pull_request_review", "workflow_run"),
        default_capability="developer.work_item",
        approval_gate="approval.required_before_repository_write",
        setup_notes=(
            "Install as a GitHub App for least-privilege repository access.",
            "Use the GitHub webhook secret as the ingress secret value.",
        ),
    ),
    "linear": ProviderTemplate(
        provider="linear",
        display_name="Linear",
        auth_model="oauth2",
        base_scopes=("read", "issues:read", "comments:read"),
        writeback_scopes=("issues:write", "comments:write"),
        webhook_events=("Issue", "Comment"),
        default_capability="project_management.work_item",
        approval_gate="approval.required_before_status_transition",
        setup_notes=(
            "Register Linear webhooks for issue and comment events.",
            "Keep team/project routing in ByteDesk policy, not in webhook glue.",
        ),
    ),
    "notion": ProviderTemplate(
        provider="notion",
        display_name="Notion",
        auth_model="oauth2",
        base_scopes=("read_content", "read_user"),
        writeback_scopes=("update_content", "insert_content"),
        webhook_events=("page.updated", "database.updated"),
        default_capability="knowledge_base.update",
        approval_gate="approval.required_before_knowledge_base_write",
        setup_notes=(
            "Use database/page ids as external object keys for idempotent sync.",
            "Treat Notion writes as system-of-record updates requiring approval.",
        ),
    ),
    "google_workspace": ProviderTemplate(
        provider="google_workspace",
        display_name="Google Workspace",
        auth_model="oauth2",
        base_scopes=(
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
        writeback_scopes=(
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.send",
        ),
        webhook_events=("drive.change", "calendar.event", "gmail.message"),
        default_capability="workspace.assistant_request",
        approval_gate="approval.required_before_email_or_calendar_write",
        setup_notes=(
            "Use domain-wide admin consent only when tenant policy allows it.",
            "Route Gmail send/calendar writebacks through human approval by default.",
        ),
    ),
    "microsoft_teams": ProviderTemplate(
        provider="microsoft_teams",
        display_name="Microsoft Teams",
        auth_model="oauth2",
        base_scopes=("ChannelMessage.Read.All", "Team.ReadBasic.All", "User.Read"),
        writeback_scopes=("ChannelMessage.Send",),
        webhook_events=("chatMessage", "channelMessage"),
        default_capability="team_chat.agent_request",
        approval_gate="approval.required_before_public_channel_write",
        setup_notes=(
            "Use Graph change notifications for channel messages.",
            "Map Teams tenant/team/channel ids into the manifest idempotency key.",
        ),
    ),
    "trello": ProviderTemplate(
        provider="trello",
        display_name="Trello",
        auth_model="oauth1",
        base_scopes=("read",),
        writeback_scopes=("write",),
        webhook_events=("card.created", "card.updated", "commentCard"),
        default_capability="project_management.work_item",
        approval_gate="approval.required_before_card_mutation",
        setup_notes=(
            "Subscribe board/card webhooks to the compiled ingress URL.",
            "Use card shortLink as the external object key.",
        ),
    ),
}


def provider_slugs() -> list[str]:
    """Return provider slugs supported by the manifest compiler."""

    return sorted(_PROVIDER_TEMPLATES)


def get_provider_template(provider: str) -> ProviderTemplate:
    """Return a provider template or raise ``ValueError`` for unknown providers."""

    key = _normalize_provider(provider)
    try:
        return _PROVIDER_TEMPLATES[key]
    except KeyError as exc:
        supported = ", ".join(provider_slugs())
        raise ValueError(f"unsupported provider '{provider}' (supported: {supported})") from exc


def compile_connected_app_manifest(
    *,
    provider: str,
    workspace_id: str,
    public_base_url: str,
    desired_capabilities: list[str] | None = None,
    tenant_id: str | None = None,
    writeback_enabled: bool = False,
) -> ConnectedAppManifest:
    """Compile a deterministic third-party connected-app install manifest.

    ``public_base_url`` is the externally reachable ByteDesk/Omnigent origin. The
    returned manifest points provider webhooks at ``/v1/ingress/{source}`` and
    OAuth redirects at ``/v1/connected-apps/oauth/{provider}/callback``.
    """

    template = get_provider_template(provider)
    workspace = _require_slugish("workspace_id", workspace_id)
    base = _normalize_public_base_url(public_base_url)
    source = f"{template.provider}-{workspace}"
    manifest_seed = f"{template.provider}:{workspace}:{tenant_id or ''}"
    manifest_id = "cam_" + hashlib.sha256(manifest_seed.encode("utf-8")).hexdigest()[:16]
    selected_capabilities = _normalize_capabilities(
        desired_capabilities, template.default_capability
    )

    scopes = list(template.base_scopes)
    if writeback_enabled:
        scopes.extend(scope for scope in template.writeback_scopes if scope not in scopes)

    ingress_path = f"/v1/ingress/{source}"
    return ConnectedAppManifest(
        manifest_id=manifest_id,
        provider=template.provider,
        display_name=template.display_name,
        workspace_id=workspace,
        tenant_id=tenant_id,
        auth_model=template.auth_model,
        required_scopes=scopes,
        webhook_events=list(template.webhook_events),
        redirect_uri=f"{base}/v1/connected-apps/oauth/{template.provider}/callback",
        ingress_path=ingress_path,
        ingress_source=source,
        secret_env_var=f"OMNIGENT_INGRESS_SECRET_{source.upper().replace('-', '_')}",
        task_defaults={
            "source": template.provider,
            "required_capability": selected_capabilities[0],
            "desired_capabilities": selected_capabilities,
            "priority": 3,
        },
        approval_gates=_approval_gates(template, writeback_enabled),
        writeback_enabled=writeback_enabled,
        idempotency_key_template=(
            f"connected-app:{template.provider}:{workspace}:{{external_object_id}}:{{event_id}}"
        ),
        bytedesk_mount={
            "oauth_callback_path": f"/v1/connected-apps/oauth/{template.provider}/callback",
            "ingress_url": f"{base}{ingress_path}",
            "task_intake_source": template.provider,
        },
        setup_notes=list(template.setup_notes),
    )


def connected_app_manifest_to_dict(manifest: ConnectedAppManifest) -> dict[str, Any]:
    """Serialize a manifest to the route's JSON response shape."""

    return asdict(manifest)


def _normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("-", "_")


def _require_slugish(name: str, value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        raise ValueError(f"{name} is required")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-.")
    if any(ch not in allowed for ch in normalized):
        raise ValueError(f"{name} may contain only letters, numbers, '-', and '.'")
    return normalized


def _normalize_public_base_url(public_base_url: str) -> str:
    base = public_base_url.strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_base_url must be an absolute http(s) URL")
    return base


def _normalize_capabilities(
    desired_capabilities: list[str] | None, default_capability: str
) -> list[str]:
    capabilities = [cap.strip() for cap in (desired_capabilities or []) if cap.strip()]
    if default_capability not in capabilities:
        capabilities.insert(0, default_capability)
    return capabilities


def _approval_gates(
    template: ProviderTemplate, writeback_enabled: bool
) -> list[dict[str, str]]:
    gates = [
        {
            "gate": "approval.required_before_autonomous_execution",
            "reason": "connected app events can trigger hosted Omnigent agents",
        }
    ]
    if writeback_enabled:
        gates.append(
            {
                "gate": template.approval_gate,
                "reason": "writeback can mutate a third-party system of record",
            }
        )
    return gates
