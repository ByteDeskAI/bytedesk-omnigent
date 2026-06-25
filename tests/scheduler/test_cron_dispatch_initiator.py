"""End-to-end cron-dispatch wiring (BDP-2347).

The native cron scheduler shipped as a durable *clock* with a log-only no-op
dispatch (``loop.py``'s ``_log_only_dispatch``) and no live ``SessionInitiator``
registered — so scheduled triggers fired but dispatched nothing in production.

These tests pin the fix: when a live ``SessionInitiator`` is registered, a due
trigger run through the tick (via the live ``build_cron_dispatch`` adapter) must
actually initiate a session for the owning agent with the trigger payload, and
the trigger's ``next_fire_at`` must still advance exactly-once. They also pin the
env-driven self-call initiator + its fail-closed factory.
"""

from __future__ import annotations

import time

import httpx

from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler, run_cron_scheduler_tick
from bytedesk_omnigent.sessions import (
    build_cron_dispatch,
    get_session_initiator,
    set_session_initiator,
)


class _SpyInitiator:
    """A live ``SessionInitiator`` that records what it was asked to dispatch."""

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
        return f"conv_{len(self.calls)}"


def test_due_trigger_initiates_a_real_session_not_log_only(tmp_path) -> None:
    """The BDP-2347 regression: a due trigger must reach the registered live
    initiator (not the silent log-only no-op), and the fire must advance once."""
    sched = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    now = int(time.time())
    sched.register_trigger(
        agent_id="maya",
        key="morning-ops-review",
        schedule_kind="interval",
        schedule_expr="86400",
        next_fire_at=now,
        payload={"prompt": "Run the morning ops review."},
        now=now,
    )

    spy = _SpyInitiator()
    try:
        set_session_initiator(spy)
        # The live extension path: dispatch is the adapter over the *registered*
        # initiator — exactly what `extension._cron_scheduler` builds.
        dispatch = build_cron_dispatch(get_session_initiator())
        fired = run_cron_scheduler_tick(sched, dispatch, now=now)
    finally:
        set_session_initiator(None)

    # A real session was initiated for the owning agent with the trigger payload.
    assert fired == 1
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["agent_id"] == "maya"
    assert call["prompt"] == "Run the morning ops review."
    assert call["source"] == "cron:morning-ops-review"
    assert call["metadata"]["trigger_key"] == "morning-ops-review"

    # Claim-once still holds: the fire instant advanced, so it is no longer due.
    assert sched.due_triggers(now=now) == []
    assert len(sched.due_triggers(now=now + 86400)) == 1


def test_self_call_initiator_creates_session_then_posts_message() -> None:
    """``HttpSelfCallInitiator`` drives the real two-call dispatch path:
    POST /v1/sessions (create) then POST /v1/sessions/{id}/events (start turn)."""
    from bytedesk_omnigent.sessions.initiate import HttpSelfCallInitiator

    seen: list[tuple[str, dict]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = {} if not request.content else __import__("json").loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "conv_xyz", "agent_id": body["agent_id"]})
        return httpx.Response(202, json={"queued": True})

    transport = httpx.MockTransport(_handler)
    initiator = HttpSelfCallInitiator(
        base_url="http://127.0.0.1:8123",
        identity_email="cron@bytedesk.local",
        transport=transport,
    )

    session_id = initiator.initiate(
        agent_id="maya",
        prompt="Run the morning ops review.",
        source="cron:morning-ops-review",
        metadata={"trigger_key": "morning-ops-review"},
        external_key="cron:morning-ops-review:123",
    )

    assert session_id == "conv_xyz"
    paths = [p for p, _ in seen]
    assert paths == ["/v1/sessions", "/v1/sessions/conv_xyz/events"]

    create_body = seen[0][1]
    assert create_body["agent_id"] == "maya"
    assert create_body["external_key"] == "cron:morning-ops-review:123"

    event_body = seen[1][1]
    assert event_body["type"] == "message"
    assert event_body["data"]["content"] == [
        {"type": "input_text", "text": "Run the morning ops review."}
    ]


def test_env_factory_returns_none_when_unconfigured(monkeypatch) -> None:
    """Fail-closed: with no self-call config the factory yields None, so the cron
    loop keeps its explicit logged fallback (tests/headless still work)."""
    from bytedesk_omnigent.sessions.initiate import build_self_call_initiator_from_env

    monkeypatch.delenv("OMNIGENT_SELF_BASE_URL", raising=False)
    assert build_self_call_initiator_from_env() is None


def test_env_factory_builds_initiator_when_configured(monkeypatch) -> None:
    from bytedesk_omnigent.sessions.initiate import (
        HttpSelfCallInitiator,
        build_self_call_initiator_from_env,
    )

    monkeypatch.setenv("OMNIGENT_SELF_BASE_URL", "http://127.0.0.1:8123")
    monkeypatch.setenv("OMNIGENT_CRON_DISPATCH_IDENTITY", "cron@bytedesk.local")
    initiator = build_self_call_initiator_from_env()
    assert isinstance(initiator, HttpSelfCallInitiator)
