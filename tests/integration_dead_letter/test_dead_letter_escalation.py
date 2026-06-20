"""Integration dead-letter escalation compiler tests (iteration 43).

The compiler turns a failed third-party webhook delivery into a deterministic
ByteDesk-facing recovery plan without carrying request bodies or secrets.
"""
from __future__ import annotations

from bytedesk_omnigent.integration_dead_letter import (
    DeadLetterIncident,
    compile_dead_letter_escalation,
)


def test_compile_dead_letter_escalation_is_deterministic_and_sanitized() -> None:
    incident = DeadLetterIncident(
        source="notion",
        match_key="page.updated",
        status="dead_lettered",
        signal_id="sig_page_sync_123",
        received_at=1_775_000_000,
        delivery_attempts=3,
        provider_event_id="evt_secret_should_not_leak",
    )

    first = compile_dead_letter_escalation(incident)
    second = compile_dead_letter_escalation(incident)

    assert first == second
    assert first["kind"] == "integration.dead_letter_escalation"
    assert first["version"] == 1
    assert first["incident_id"] == "idle_notion_page-updated_sig-page-sync-123_762b079e"
    assert first["source"] == "notion"
    assert first["match_key"] == "page.updated"
    assert first["status"] == "dead_lettered"
    assert first["severity"] == "high"
    assert first["byte_desk_task"] == {
        "title": "Recover notion page.updated webhook delivery",
        "owner": "integration-ops",
        "priority": "P1",
        "labels": [
            "integration:notion",
            "event:page.updated",
            "status:dead_lettered",
            "signal:sig_page_sync_123",
        ],
    }
    assert first["workflow"] == [
        "Correlate the provider event in the external system without copying secrets.",
        "Verify the Omnigent webhook binding for notion/page.updated points at sig_page_sync_123.",
        "Check whether a session is still waiting for sig_page_sync_123 "
        "or needs a new recovery session.",
        "Replay the event once the binding or waiting session is repaired.",
    ]
    assert first["retry_policy"] == {
        "strategy": "manual_replay_after_repair",
        "max_replays": 1,
        "backoff_seconds": [300, 900, 1800],
    }
    assert "evt_secret_should_not_leak" not in repr(first)


def test_compile_dead_letter_escalation_maps_signature_failures_to_security() -> None:
    plan = compile_dead_letter_escalation(
        DeadLetterIncident(
            source="github",
            match_key="issues.opened",
            status="bad_signature",
            signal_id=None,
            received_at=1_775_000_100,
        )
    )

    assert plan["severity"] == "critical"
    assert plan["byte_desk_task"]["owner"] == "security-ops"
    assert plan["byte_desk_task"]["priority"] == "P0"
    assert (
        plan["workflow"][1]
        == "Rotate or re-sync the github webhook signing secret before replay."
    )
    assert "signal:" not in " ".join(plan["byte_desk_task"]["labels"])
