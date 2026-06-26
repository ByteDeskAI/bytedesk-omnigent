"""Deterministic third-party credential rotation plans for connected apps.

The compiler in this module is intentionally pure: it turns metadata about an
existing connected-app credential into a secret-free runbook that Omnigent agents
or ByteDesk Platform workers can execute. It never accepts or serializes raw
secret values; it carries only vault references and version labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CredentialKind = Literal["webhook_secret", "oauth_client_secret", "api_token"]

_MAX_ATTEMPTS = 8
_MIN_ATTEMPTS = 1


@dataclass(frozen=True)
class CredentialRotationTarget:
    """Metadata needed to compile a credential-rotation runbook.

    :param service: Human/provider name, normalized to a stable slug.
    :param app_id: Connected-app identifier inside ByteDesk/Omnigent.
    :param credential_kind: Credential category being rotated.
    :param environment: Deployment environment for approval and idempotency.
    :param current_version: Current vault/provider version label.
    :param next_version: Next vault/provider version label to activate.
    :param vault_ref: Secret-manager reference, not secret material.
    :param owner_team: Owning team for routing/audit tasks.
    """

    service: str
    app_id: str
    credential_kind: CredentialKind
    environment: str
    current_version: str
    next_version: str
    vault_ref: str
    owner_team: str = "integrations"


def compile_credential_rotation_plan(
    target: CredentialRotationTarget,
    *,
    max_attempts: int = 5,
) -> dict[str, object]:
    """Compile a deterministic, secret-free credential rotation plan.

    The returned dict is JSON-serializable and suitable for a ByteDesk Platform
    task, an Omnigent autonomous workflow, or a human-reviewed runbook. Retry
    attempts are bounded so generated plans cannot ask agents to loop forever.
    """
    service = _slug(target.service)
    environment = _slug(target.environment)
    attempts = min(_MAX_ATTEMPTS, max(_MIN_ATTEMPTS, int(max_attempts)))
    approval_reasons = _approval_reasons(target.credential_kind, environment)
    idempotency_key = (
        f"credential-rotation:{service}:{target.app_id}:{target.credential_kind}:"
        f"{environment}:{target.current_version}->{target.next_version}"
    )

    return {
        "plan_type": "credential_rotation",
        "service": service,
        "app_id": target.app_id,
        "credential_kind": target.credential_kind,
        "environment": environment,
        "owner_team": target.owner_team,
        "current_version": target.current_version,
        "next_version": target.next_version,
        "vault_ref": target.vault_ref,
        "secret_material_included": False,
        "idempotency_key": idempotency_key,
        "max_attempts": attempts,
        "requires_human_approval": bool(approval_reasons),
        "approval_reasons": approval_reasons,
        "steps": _steps(target, service, environment),
        "rollback": {
            "safe_until_step": "revoke_previous_credential",
            "actions": [
                "restore_provider_reference_to_current_version",
                "restore_omnigent_binding_to_current_version",
                "re-run_shadow_auth_probe",
                "record_rotation_aborted",
            ],
        },
        "audit": {
            "dedupe_key": idempotency_key,
            "labels": [
                "integration",
                "credential-rotation",
                f"service:{service}",
                f"environment:{environment}",
                f"kind:{target.credential_kind}",
            ],
        },
    }


def _approval_reasons(kind: CredentialKind, environment: str) -> list[str]:
    reasons: list[str] = []
    if environment == "production":
        reasons.append("production_rotation")
    if kind == "oauth_client_secret":
        reasons.append("oauth_client_secret_rotation")
    return reasons


def _steps(
    target: CredentialRotationTarget,
    service: str,
    environment: str,
) -> list[dict[str, object]]:
    return [
        {
            "id": "prepare",
            "action": "snapshot_current_binding",
            "description": "Record current provider and Omnigent binding versions.",
        },
        {
            "id": "install_next_credential",
            "action": "write_next_version_reference",
            "description": "Install the next credential by vault reference only.",
            "vault_ref": target.vault_ref,
            "version": target.next_version,
        },
        {
            "id": "verify_shadow_auth",
            "action": "run_shadow_auth_probe",
            "description": "Probe provider auth without cutting traffic over.",
            "expected_service": service,
            "environment": environment,
        },
        {
            "id": "cutover",
            "action": "activate_next_version",
            "description": "Switch Omnigent/provider binding to the verified next version.",
            "from_version": target.current_version,
            "to_version": target.next_version,
        },
        {
            "id": "revoke_previous_credential",
            "action": "revoke_current_version",
            "description": "Revoke the old credential only after cutover succeeds.",
            "version": target.current_version,
        },
        {
            "id": "audit",
            "action": "record_rotation_complete",
            "description": "Persist completion evidence for ByteDesk governance.",
        },
    ]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"
