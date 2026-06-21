"""Deterministic recovery plans for failed integration ingress events.

This module is intentionally pure: it turns a minimal, sanitized incident record
into a ByteDesk-facing task + workflow plan that operators or autonomous
supervisors can enqueue without replaying secrets, request bodies, or raw provider
payloads. It complements the webhook ingress path by giving dead-lettered and
expired deliveries an explicit recovery artifact instead of a log-only failure.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

_ESCALATION_KIND = "integration.dead_letter_escalation"
_ESCALATION_VERSION = 1

_STATUS_SEVERITY: dict[str, tuple[str, str, str]] = {
    "bad_signature": ("critical", "security-ops", "P0"),
    "expired": ("high", "integration-ops", "P1"),
    "dead_lettered": ("high", "integration-ops", "P1"),
    "no_binding": ("medium", "integration-ops", "P2"),
    "already_resolved": ("low", "integration-ops", "P3"),
}


@dataclass(frozen=True)
class DeadLetterIncident:
    """Minimal sanitized description of a failed integration delivery.

    ``provider_event_id`` is accepted so callers can correlate externally, but it
    is intentionally excluded from the returned plan to avoid leaking provider
    identifiers or secret-bearing payload fragments into ByteDesk task metadata.
    """

    source: str
    match_key: str
    status: str
    signal_id: str | None
    received_at: int
    delivery_attempts: int = 1
    provider_event_id: str | None = None


def compile_dead_letter_escalation(incident: DeadLetterIncident) -> dict[str, Any]:
    """Compile a deterministic ByteDesk recovery plan for an ingress failure.

    The output is JSON-serializable and stable for the same incident, which lets
    autonomous supervisors de-dupe or upsert recovery tasks. It deliberately
    carries only route metadata (source, event, status, optional signal id) and a
    prescriptive workflow; callers should not attach raw webhook bodies or
    secrets to this artifact.
    """

    severity, owner, priority = _STATUS_SEVERITY.get(
        incident.status, ("medium", "integration-ops", "P2")
    )
    labels = [
        f"integration:{incident.source}",
        f"event:{incident.match_key}",
        f"status:{incident.status}",
    ]
    if incident.signal_id:
        labels.append(f"signal:{incident.signal_id}")

    return {
        "kind": _ESCALATION_KIND,
        "version": _ESCALATION_VERSION,
        "incident_id": _incident_id(incident),
        "source": incident.source,
        "match_key": incident.match_key,
        "status": incident.status,
        "severity": severity,
        "received_at": incident.received_at,
        "delivery_attempts": max(1, incident.delivery_attempts),
        "byte_desk_task": {
            "title": f"Recover {incident.source} {incident.match_key} webhook delivery",
            "owner": owner,
            "priority": priority,
            "labels": labels,
        },
        "workflow": _workflow(incident),
        "retry_policy": {
            "strategy": "manual_replay_after_repair",
            "max_replays": 1,
            "backoff_seconds": [300, 900, 1800],
        },
    }


def _workflow(incident: DeadLetterIncident) -> list[str]:
    source = incident.source
    match_key = incident.match_key
    signal_id = incident.signal_id or "the intended signal"
    second_step = (
        f"Rotate or re-sync the {source} webhook signing secret before replay."
        if incident.status == "bad_signature"
        else f"Verify the Omnigent webhook binding for {source}/{match_key} points at {signal_id}."
    )
    return [
        "Correlate the provider event in the external system without copying secrets.",
        second_step,
        f"Check whether a session is still waiting for {signal_id} "
        "or needs a new recovery session.",
        "Replay the event once the binding or waiting session is repaired.",
    ]


def _incident_id(incident: DeadLetterIncident) -> str:
    raw = "|".join(
        [
            incident.source,
            incident.match_key,
            incident.status,
            incident.signal_id or "none",
            str(incident.received_at),
            str(max(1, incident.delivery_attempts)),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return "_".join(
        [
            "idle",
            _slug(incident.source),
            _slug(incident.match_key),
            _slug(incident.signal_id or "no-signal"),
            digest,
        ]
    )


def _slug(value: str) -> str:
    lowered = value.strip().lower().replace(".", "-").replace("_", "-")
    cleaned = re.sub(r"[^a-z0-9-]+", "-", lowered)
    return re.sub(r"-+", "-", cleaned).strip("-") or "unknown"


__all__ = ["DeadLetterIncident", "compile_dead_letter_escalation"]
