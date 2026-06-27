"""Deterministic tool contracts for integration-capability agents.

The integration catalog says which third-party capabilities matter. This module
turns a catalog entry into the minimal tool surface an autonomous agent should
receive before operating that capability inside ByteDesk/Omnigent.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import get_integration_capability
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

ToolOperation = Literal[
    "read_context",
    "normalize_event",
    "execute_action",
    "record_evidence",
]


@dataclass(frozen=True)
class IntegrationToolContract:
    """One agent-callable tool contract for an integration capability."""

    name: str
    operation: ToolOperation
    description: str
    required_inputs: tuple[str, ...]
    required_scopes: tuple[str, ...]
    approval_required: bool
    category_policy_gate: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["required_inputs"] = list(self.required_inputs)
        data["required_scopes"] = list(self.required_scopes)
        return data


_CATEGORY_POLICY_GATE = {
    "communication": "communication-loop",
    "project_management": "work-item-lifecycle",
    "knowledge": "knowledge-scope-control",
    "developer": "developer-change-safety",
    "crm_support": "customer-record-safety",
    "commerce_billing": "revenue-mutation-safety",
    "workflow_harness": "workflow-determinism",
}

_AGENT_BLUEPRINT_HINTS = (
    "Grant read-context tools to intake and triage agents first.",
    "Reserve execute-action tools for agents with explicit policy gates.",
    "Bind every provider mutation to outcome evidence before completing the task.",
)


def _tool_prefix(slug: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")


def _write_scopes(required_scopes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        scope
        for scope in required_scopes
        if "write" in scope.lower()
        or scope.endswith(".write")
        or scope in {"commands", "insert_content", "update_content"}
    )


def _read_scopes(required_scopes: tuple[str, ...]) -> tuple[str, ...]:
    write_scopes = set(_write_scopes(required_scopes))
    return tuple(scope for scope in required_scopes if scope not in write_scopes)


def compile_integration_tool_contract(slug: str) -> dict | None:
    """Return a JSON-ready least-privilege tool contract for one capability."""

    capability = get_integration_capability(slug)
    matrix = compile_integration_verification_matrix(slug)
    if capability is None or matrix is None:
        return None

    prefix = _tool_prefix(capability.slug)
    category_gate = _CATEGORY_POLICY_GATE[capability.category]
    read_scopes = _read_scopes(capability.required_scopes)
    write_scopes = _write_scopes(capability.required_scopes)

    tools = [
        IntegrationToolContract(
            name=f"{prefix}.read_context",
            operation="read_context",
            description=(
                "Fetch provider context needed for task intake without mutating the "
                "external system."
            ),
            required_inputs=("tenant_id", "external_object_id"),
            required_scopes=read_scopes,
            approval_required=False,
            category_policy_gate=category_gate,
        ),
        IntegrationToolContract(
            name=f"{prefix}.normalize_event",
            operation="normalize_event",
            description="Convert provider events or records into deterministic Omnigent signals.",
            required_inputs=("tenant_id", "event_id", "payload"),
            required_scopes=(),
            approval_required=False,
            category_policy_gate=category_gate,
        ),
    ]

    if matrix["risk_tier"] == "external_write":
        tools.append(
            IntegrationToolContract(
                name=f"{prefix}.execute_action",
                operation="execute_action",
                description=(
                    "Perform a provider-side write only after policy approval and "
                    "dry-run evidence exist."
                ),
                required_inputs=(
                    "tenant_id",
                    "task_id",
                    "action",
                    "approval_id",
                    "dry_run",
                    "idempotency_key",
                ),
                required_scopes=write_scopes,
                approval_required=True,
                category_policy_gate=category_gate,
            )
        )

    tools.append(
        IntegrationToolContract(
            name=f"{prefix}.record_evidence",
            operation="record_evidence",
            description=(
                "Persist safe execution receipts that connect provider objects back "
                "to Omnigent tasks and agents."
            ),
            required_inputs=("tenant_id", "task_id", "provider_object_id", "outcome"),
            required_scopes=(),
            approval_required=False,
            category_policy_gate=category_gate,
        )
    )

    return {
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": matrix["risk_tier"],
        "auth_model": capability.auth_model,
        "tools": [tool.to_dict() for tool in tools],
        "agent_blueprint_hints": list(_AGENT_BLUEPRINT_HINTS),
    }
