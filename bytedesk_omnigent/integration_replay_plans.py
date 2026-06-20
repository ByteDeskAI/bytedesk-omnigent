"""Deterministic replay plans for connected-app integration events.

The compiler is pure so ByteDesk Platform can preview retry, dedupe, approval,
and dead-letter behavior before enabling a third-party connector.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_CUSTOMER_RECORD_PROVIDERS = {
    "hubspot",
    "salesforce",
    "zendesk",
    "intercom",
    "stripe",
    "shopify",
}
_SYSTEM_OF_RECORD_OPERATIONS = {
    "update_crm_record",
    "update_customer_record",
    "send_email",
    "refund_payment",
    "cancel_subscription",
    "create_invoice",
    "writeback",
}
_COLLABORATION_PROVIDERS = {"slack", "discord", "microsoft-teams", "teams"}


def _slug(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "-").replace("_", "-")


def _missing_required(request: Mapping[str, Any]) -> list[str]:
    required = ["provider", "workspace_id", "event_type", "operation"]
    return [field for field in required if not str(request.get(field) or "").strip()]


def compile_integration_replay_plan(request: Mapping[str, Any]) -> dict[str, Any]:
    """Compile a deterministic replay-safety plan for one integration event.

    Required request keys: ``provider``, ``workspace_id``, ``event_type``, and
    ``operation``. ``external_id`` is optional and defaults to ``*`` for providers
    that cannot expose a stable resource id during setup previews.
    """

    missing = _missing_required(request)
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    provider = _slug(request["provider"])
    workspace_id = str(request["workspace_id"]).strip()
    event_type = str(request["event_type"]).strip()
    operation = _slug(request["operation"])
    external_id = str(request.get("external_id") or "*").strip()
    writeback = bool(request.get("writeback"))

    high_risk = (
        writeback
        or (
            provider in _CUSTOMER_RECORD_PROVIDERS
            and operation in _SYSTEM_OF_RECORD_OPERATIONS
        )
    )
    medium_risk = not high_risk and provider not in _COLLABORATION_PROVIDERS

    risk_level = "high" if high_risk else "medium" if medium_risk else "low"
    requires_approval = risk_level == "high"
    replay_strategy = (
        "dedupe_then_manual_review" if requires_approval else "dedupe_then_dispatch"
    )

    steps = [
        {
            "id": "normalize_event",
            "description": "Canonicalize provider, event, workspace, and external resource ids.",
        },
        {
            "id": "dedupe_event",
            "description": "Acquire the idempotency key before waking an Omnigent agent.",
        },
        {
            "id": "verify_binding",
            "description": (
                "Resolve the configured source/event binding to the parked "
                "Omnigent signal."
            ),
        },
    ]
    if requires_approval:
        steps.append(
            {
                "id": "approval_gate",
                "description": (
                    "Require operator review before replaying a customer/"
                    "system-of-record write."
                ),
            }
        )
    steps.extend(
        [
            {
                "id": "dispatch_agent",
                "description": "Deliver the signal or create the work item exactly once.",
            },
            {
                "id": "record_receipt",
                "description": (
                    "Persist replay outcome metadata for audit and support diagnostics."
                ),
            },
        ]
    )
    if writeback:
        steps.append(
            {
                "id": "writeback",
                "description": (
                    "Apply the approved provider writeback with the same "
                    "idempotency key."
                ),
            }
        )

    return {
        "provider": provider,
        "workspace_id": workspace_id,
        "event_type": event_type,
        "operation": operation,
        "external_id": external_id,
        "idempotency_key": (
            f"integration-replay:v1:{provider}:{workspace_id}:{event_type}:{external_id}"
        ),
        "replay_strategy": replay_strategy,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "retry_policy": {
            "max_attempts": 3,
            "backoff": "exponential",
            "base_delay_seconds": 30,
        },
        "dead_letter": {
            "recommended_queue": f"integration.{provider}.dead_letter",
            "manual_review_required": requires_approval,
        },
        "steps": steps,
    }
