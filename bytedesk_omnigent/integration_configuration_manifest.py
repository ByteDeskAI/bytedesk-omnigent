"""Deterministic configuration manifests for integration capability setup.

The integration capability catalog describes what Omnigent should build next.
This module turns a catalog entry into the inert, secret-value-free deployment
configuration slots that ByteDesk Platform or an autonomous setup agent must
collect before enabling that integration for a tenant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)


@dataclass(frozen=True)
class ConfigurationSlot:
    """One configuration key required to activate a catalog capability."""

    key: str
    label: str
    required: bool
    secret: bool
    purpose: str

    def to_dict(self) -> dict:
        return asdict(self)


_CATEGORY_NOTES: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "Verify provider signatures before accepting collaboration events.",
        "Route outbound messages through workspace-level rate limits.",
    ),
    "project_management": (
        "Preserve external work item identifiers for idempotent task sync.",
        "Keep status write-back behind a source-of-truth conflict policy.",
    ),
    "knowledge": (
        "Restrict reads to explicitly selected documents, drives, pages, or databases.",
        "Record write provenance with source task and agent identifiers.",
    ),
    "developer": (
        "Prefer installation-scoped app credentials over user personal tokens.",
        "Route code or CI mutations through reviewable pull requests.",
    ),
    "crm_support": (
        "Gate public customer replies until quality and approval policies pass.",
        "Capture before and after summaries for customer record mutations.",
    ),
    "commerce_billing": (
        "Start with read-only commerce context before enabling revenue mutations.",
        "Require explicit approval for refunds, cancellations, and payment-side effects.",
    ),
    "workflow_harness": (
        "Validate blueprint schema before admitting a workflow template.",
        "Store run artifacts where task, agent, and phase evidence can be correlated.",
    ),
}


def compile_integration_configuration_manifest(slug: str) -> dict | None:
    """Return a JSON-ready, secret-value-free setup manifest for one capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    prefix = _configuration_prefix(capability.slug)
    slots = _slots_for_capability(
        prefix=prefix,
        auth_model=capability.auth_model,
        category=capability.category,
    )
    slot_dicts = [slot.to_dict() for slot in slots]
    return {
        "object": "integration_configuration_manifest",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "auth_model": capability.auth_model,
        "configuration_keys": [slot.key for slot in slots],
        "slots": slot_dicts,
        "minimum_required_slots": sum(1 for slot in slots if slot.required),
        "deployment_notes": list(_CATEGORY_NOTES[capability.category]),
    }


def _configuration_prefix(slug: str) -> str:
    return "".join(char.upper() if char.isalnum() else "_" for char in slug)


def _slots_for_capability(
    *, prefix: str, auth_model: str, category: CapabilityCategory
) -> tuple[ConfigurationSlot, ...]:
    if category == "workflow_harness":
        return (
            ConfigurationSlot(
                key=f"{prefix}_BLUEPRINT_REPOSITORY",
                label="Workflow blueprint repository",
                required=True,
                secret=False,
                purpose="Location of approved workflow blueprint definitions.",
            ),
            ConfigurationSlot(
                key=f"{prefix}_SCHEMA_VERSION",
                label="Workflow schema version",
                required=True,
                secret=False,
                purpose="Deterministic schema version used to validate blueprint phases.",
            ),
            ConfigurationSlot(
                key=f"{prefix}_ARTIFACT_BUCKET",
                label="Workflow artifact bucket",
                required=True,
                secret=False,
                purpose="Evidence store for phase outputs, logs, and verification artifacts.",
            ),
        )

    slots: list[ConfigurationSlot] = []
    if "oauth" in auth_model.lower():
        slots.extend(
            (
                ConfigurationSlot(
                    key=f"{prefix}_CLIENT_ID",
                    label="OAuth client id",
                    required=True,
                    secret=False,
                    purpose="Provider-issued public client identifier for the connected app.",
                ),
                ConfigurationSlot(
                    key=f"{prefix}_CLIENT_SECRET",
                    label="OAuth client secret",
                    required=True,
                    secret=True,
                    purpose="Provider-issued secret used only during token exchange or refresh.",
                ),
                ConfigurationSlot(
                    key=f"{prefix}_REDIRECT_URI",
                    label="OAuth redirect URI",
                    required=True,
                    secret=False,
                    purpose="ByteDesk callback URI registered with the provider.",
                ),
            )
        )

    webhook_categories: set[CapabilityCategory] = {
        "communication",
        "project_management",
        "developer",
        "crm_support",
        "commerce_billing",
    }
    if category in webhook_categories:
        slots.append(
            ConfigurationSlot(
                key=f"{prefix}_SIGNING_SECRET",
                label="Webhook signing secret",
                required=True,
                secret=True,
                purpose=(
                    "Secret used to verify provider webhook signatures before ingress "
                    "normalization."
                ),
            )
        )
        slots.append(
            ConfigurationSlot(
                key=f"{prefix}_WEBHOOK_BASE_URL",
                label="Webhook base URL",
                required=True,
                secret=False,
                purpose="Public HTTPS origin where provider webhooks deliver tenant events.",
            )
        )

    return tuple(slots)
