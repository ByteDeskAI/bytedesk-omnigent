"""Tests for the sys_session_initiate seam: the registry + the cron-dispatch
adapter (BDP-2279 α3b, ADR-0142)."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from bytedesk_omnigent.lifecycle import WorkflowLifecycleStatus
from bytedesk_omnigent.scheduler.scheduler import CronTrigger
from bytedesk_omnigent.sessions import (
    build_cron_dispatch,
    get_session_initiator,
    set_session_initiator,
)
from bytedesk_omnigent.task_execution import TaskDispatch
from bytedesk_omnigent.tasks import Task


class _RecordingInitiator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def initiate(
        self,
        *,
        agent_id,
        prompt,
        source,
        metadata=None,
        external_key=None,
    ) -> str:
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "source": source,
                "metadata": metadata,
                "external_key": external_key,
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
    assert call["external_key"] == "cron:cron_1:0"


def test_build_cron_dispatch_skips_when_task_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initiator = _RecordingInitiator()
    dispatch = build_cron_dispatch(initiator)
    store = MagicMock()
    store.get_task.return_value = None
    monkeypatch.setattr(
        "bytedesk_omnigent.tasks.store.get_task_store",
        lambda: store,
    )

    dispatch(_trigger(payload={"task_id": "task_missing"}))

    assert initiator.calls == []
    store.get_task.assert_called_once_with("task_missing")


def test_build_cron_dispatch_runs_task_for_task_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initiator = _RecordingInitiator()
    dispatch = build_cron_dispatch(initiator)
    task = Task(
        id="task_ops",
        title="Run nightly checks",
        owner_agent_id=None,
        assignee_agent_id=None,
        required_capability=None,
        status=WorkflowLifecycleStatus.OPEN,
        priority=1,
        source="cron",
        payload={"prompt": "Check all services."},
        created_at=0,
        updated_at=0,
    )
    store = MagicMock()
    store.get_task.return_value = task
    monkeypatch.setattr(
        "bytedesk_omnigent.tasks.store.get_task_store",
        lambda: store,
    )
    run_calls: list[dict] = []

    def _run_task(task_arg, *, initiator=None, external_key=None, resolve=None):
        run_calls.append(
            {
                "task": task_arg,
                "initiator": initiator,
                "external_key": external_key,
            }
        )
        return TaskDispatch(
            task_id="task_ops",
            agent_id="ag_runner",
            session_id="sess_task",
        )

    monkeypatch.setattr("bytedesk_omnigent.task_execution.run_task", _run_task)

    dispatch(
        _trigger(
            agent_id="ag_default",
            payload={"task_id": "task_ops", "run_as_agent_id": "ag_runner"},
        )
    )

    assert initiator.calls == []
    assert len(run_calls) == 1
    assert run_calls[0]["initiator"] is initiator
    assert run_calls[0]["external_key"] == "cron:cron_1:0"
    assert run_calls[0]["task"] == replace(task, owner_agent_id="ag_runner")


def test_build_cron_dispatch_falls_back_to_self_describing_prompt() -> None:
    initiator = _RecordingInitiator()
    dispatch = build_cron_dispatch(initiator)

    # No payload prompt → a deterministic, non-empty seed prompt.
    dispatch(_trigger(key="weekly-digest", payload=None))
    dispatch(_trigger(key="weekly-digest", payload={"prompt": "   "}))
    dispatch(_trigger(key="weekly-digest", payload={"prompt": 42}))

    assert [c["prompt"] for c in initiator.calls] == [
        "Scheduled trigger fired: weekly-digest",
        "Scheduled trigger fired: weekly-digest",
        "Scheduled trigger fired: weekly-digest",
    ]


def test_build_self_call_initiator_from_env_returns_none_when_unset(
    monkeypatch,
) -> None:
    from bytedesk_omnigent.sessions.initiate import build_self_call_initiator_from_env

    monkeypatch.delenv("OMNIGENT_SELF_BASE_URL", raising=False)
    assert build_self_call_initiator_from_env() is None


def test_build_self_call_initiator_from_env_honors_identity(
    monkeypatch,
) -> None:
    from bytedesk_omnigent.sessions.initiate import (
        HttpSelfCallInitiator,
        build_self_call_initiator_from_env,
    )

    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://127.0.0.1:8123/")
    monkeypatch.setenv("OMNIGENT_CRON_DISPATCH_IDENTITY", "cron@example.com")
    initiator = build_self_call_initiator_from_env()
    assert isinstance(initiator, HttpSelfCallInitiator)
    assert initiator._base_url == "http://127.0.0.1:8123"
    assert initiator._identity_email == "cron@example.com"


def test_http_self_call_initiator_creates_session_and_posts_event() -> None:
    import httpx

    from bytedesk_omnigent.sessions.initiate import HttpSelfCallInitiator

    calls: list[tuple[str, str, object]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "sess_42"})
        if request.method == "POST" and request.url.path == "/v1/sessions/sess_42/events":
            return httpx.Response(202, json={"status": "accepted"})
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    session_id = HttpSelfCallInitiator(
        base_url="http://127.0.0.1:8123",
        identity_email="dispatch@example.com",
        transport=transport,
    ).initiate(
        agent_id="ag_ops",
        prompt="Run nightly checks.",
        source="cron:nightly",
        metadata={"trigger_id": "cron_9"},
        external_key="ext_1",
    )

    assert session_id == "sess_42"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/v1/sessions"
    assert calls[1][1] == "/v1/sessions/sess_42/events"
