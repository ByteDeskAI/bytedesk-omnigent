"""Tests for the sys_session_initiate seam: the registry + the cron-dispatch
adapter (BDP-2279 α3b, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.scheduler.scheduler import CronTrigger
from bytedesk_omnigent.sessions import (
    build_cron_dispatch,
    get_session_initiator,
    set_session_initiator,
)


class _RecordingInitiator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def initiate(self, *, agent_id, prompt, source, metadata=None) -> str:
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "source": source,
                "metadata": metadata,
            }
        )
        return f"sess_{len(self.calls)}"


def _trigger(*, agent_id="ag_1", key="daily-standup", payload=None) -> CronTrigger:
    return CronTrigger(
        id="cron_1",
        agent_id=agent_id,
        key=key,
        schedule_kind="interval",
        schedule_expr="86400",
        next_fire_at=0,
        enabled=True,
        payload=payload,
    )


def test_registry_set_and_get_round_trip() -> None:
    initiator = _RecordingInitiator()
    try:
        assert get_session_initiator() is None
        set_session_initiator(initiator)
        assert get_session_initiator() is initiator
    finally:
        set_session_initiator(None)
    assert get_session_initiator() is None


def test_build_cron_dispatch_initiates_session_with_payload_prompt() -> None:
    initiator = _RecordingInitiator()
    dispatch = build_cron_dispatch(initiator)

    dispatch(_trigger(payload={"prompt": "Run the morning ops review."}))

    assert len(initiator.calls) == 1
    call = initiator.calls[0]
    assert call["agent_id"] == "ag_1"
    assert call["prompt"] == "Run the morning ops review."
    assert call["source"] == "cron:daily-standup"
    assert call["metadata"] == {"trigger_id": "cron_1", "trigger_key": "daily-standup"}


def test_build_cron_dispatch_falls_back_to_self_describing_prompt() -> None:
    initiator = _RecordingInitiator()
    dispatch = build_cron_dispatch(initiator)

    # No payload prompt → a deterministic, non-empty seed prompt.
    dispatch(_trigger(key="weekly-digest", payload=None))
    dispatch(_trigger(key="weekly-digest", payload={"prompt": "   "}))

    assert [c["prompt"] for c in initiator.calls] == [
        "Scheduled trigger fired: weekly-digest",
        "Scheduled trigger fired: weekly-digest",
    ]
