"""Validate deterministic integration workflow blueprints.

This module gives autonomous loops and ByteDesk Platform a pure, secret-free
preflight for Archon-style phase graphs before a catalog integration is handed to
agents for execution. The validator intentionally accepts ordinary dictionaries
so API routes, YAML loaders, and future UI forms can share the same contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from bytedesk_omnigent.integration_capabilities import get_integration_capability

IssueSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class BlueprintValidationIssue:
    """One deterministic workflow-blueprint validation issue."""

    code: str
    detail: str
    severity: IssueSeverity = "error"
    phase_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_integration_workflow_blueprint(blueprint: dict[str, Any]) -> dict:
    """Return a JSON-ready validation report for an integration workflow graph."""

    issues: list[BlueprintValidationIssue] = []
    capability_slug = _optional_string(blueprint.get("capability_slug"))
    if capability_slug is None:
        capability_slug = _optional_string(blueprint.get("slug"))

    if capability_slug is None:
        issues.append(
            BlueprintValidationIssue(
                code="missing_capability_slug",
                detail="blueprint must include capability_slug for catalog traceability",
            )
        )
    elif get_integration_capability(capability_slug) is None:
        issues.append(
            BlueprintValidationIssue(
                code="unknown_capability",
                detail=f"unknown integration capability: {capability_slug}",
            )
        )

    raw_phases = blueprint.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        issues.append(
            BlueprintValidationIssue(
                code="missing_phases",
                detail="blueprint must include at least one workflow phase",
            )
        )
        raw_phases = []

    phase_ids: list[str] = []
    dependencies: dict[str, list[str]] = {}
    seen: set[str] = set()

    for index, raw_phase in enumerate(raw_phases):
        if not isinstance(raw_phase, dict):
            issues.append(
                BlueprintValidationIssue(
                    code="invalid_phase",
                    detail=f"phase at index {index} must be an object",
                )
            )
            continue

        phase_id = _optional_string(raw_phase.get("id"))
        issue_phase_id = phase_id or f"index:{index}"
        if phase_id is None:
            issues.append(
                BlueprintValidationIssue(
                    code="missing_phase_id",
                    detail="phase must include a stable id",
                    phase_id=issue_phase_id,
                )
            )
            continue

        phase_ids.append(phase_id)
        if not _is_stable_node_id(phase_id):
            issues.append(
                BlueprintValidationIssue(
                    code="unstable_phase_id",
                    detail="phase id must use lowercase letters, numbers, hyphens, or underscores",
                    phase_id=phase_id,
                )
            )
        if phase_id in seen:
            issues.append(
                BlueprintValidationIssue(
                    code="duplicate_phase_id",
                    detail=f"phase id is declared more than once: {phase_id}",
                    phase_id=phase_id,
                )
            )
        seen.add(phase_id)

        _require_non_empty_string(raw_phase, "role", phase_id, issues)
        _require_non_empty_list(raw_phase, "inputs", phase_id, issues)
        _require_non_empty_list(raw_phase, "outputs", phase_id, issues)
        _require_non_empty_list(raw_phase, "completion_evidence", phase_id, issues)
        dependencies.setdefault(phase_id, []).extend(_string_list(raw_phase.get("depends_on")))

    known_phase_ids = set(phase_ids)
    for phase_id, depends_on in dependencies.items():
        for dependency in depends_on:
            if dependency == phase_id:
                issues.append(
                    BlueprintValidationIssue(
                        code="self_dependency",
                        detail=f"phase cannot depend on itself: {phase_id}",
                        phase_id=phase_id,
                    )
                )
            elif dependency not in known_phase_ids:
                issues.append(
                    BlueprintValidationIssue(
                        code="missing_dependency",
                        detail=f"phase depends on missing phase: {dependency}",
                        phase_id=phase_id,
                    )
                )

    if _has_cycle(dependencies):
        issues.append(
            BlueprintValidationIssue(
                code="cycle_detected",
                detail="phase dependency graph must be acyclic",
            )
        )

    error_count = sum(1 for issue in issues if issue.severity == "error")
    return {
        "object": "integration_workflow_blueprint_validation",
        "capability_slug": capability_slug,
        "valid": error_count == 0,
        "phase_count": len(raw_phases),
        "deterministic_node_ids": phase_ids,
        "issue_count": len(issues),
        "issues": [issue.to_dict() for issue in issues],
    }


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _is_stable_node_id(value: str) -> bool:
    return all(char.islower() or char.isdigit() or char in "-_" for char in value)


def _require_non_empty_string(
    phase: dict[str, Any],
    field: str,
    phase_id: str,
    issues: list[BlueprintValidationIssue],
) -> None:
    if _optional_string(phase.get(field)) is None:
        issues.append(
            BlueprintValidationIssue(
                code=f"missing_{field}",
                detail=f"phase must include non-empty {field}",
                phase_id=phase_id,
            )
        )


def _require_non_empty_list(
    phase: dict[str, Any],
    field: str,
    phase_id: str,
    issues: list[BlueprintValidationIssue],
) -> None:
    if not _string_list(phase.get(field)):
        issues.append(
            BlueprintValidationIssue(
                code=f"missing_{field}",
                detail=f"phase must include at least one {field} entry",
                phase_id=phase_id,
            )
        )


def _has_cycle(dependencies: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(phase_id: str) -> bool:
        if phase_id in visiting:
            return True
        if phase_id in visited:
            return False
        visiting.add(phase_id)
        for dependency in dependencies.get(phase_id, []):
            if dependency in dependencies and visit(dependency):
                return True
        visiting.remove(phase_id)
        visited.add(phase_id)
        return False

    return any(visit(phase_id) for phase_id in dependencies)
