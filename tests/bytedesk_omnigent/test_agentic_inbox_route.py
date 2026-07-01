"""Route tests for Agentic Inbox email webhooks (BDP-2455)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import bytedesk_omnigent.agentic_inbox as inbox
import bytedesk_omnigent.sessions as sessions
from bytedesk_omnigent.routes.agentic_inbox import create_agentic_inbox_router


def _sign(raw_body: bytes, secret: str, timestamp: str) -> str:
    signed = timestamp.encode("utf-8") + b"." + raw_body
    return "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_agentic_inbox_router(), prefix="/v1")
    return TestClient(app)


def test_agentic_inbox_route_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    response = _client().post(
        "/v1/agentic-inbox/events",
        json={"event_id": "evt_1"},
        headers={
            "X-Omnigent-Timestamp": str(int(time.time())),
            "X-Omnigent-Signature": "bad",
        },
    )

    assert response.status_code == 401
    assert response.json()["status"] == "bad_signature"


def test_agentic_inbox_route_accepts_signed_event(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")

    class _Resolver:
        def __init__(self, *_args) -> None:
            pass

        def resolve_agent_id(self, mailbox_id: str) -> str:
            return "ag_maya"

    monkeypatch.setattr(inbox, "AgenticInboxResolver", _Resolver)
    monkeypatch.setattr(inbox, "get_agentic_inbox_event_store", lambda: object())
    monkeypatch.setattr(sessions, "get_session_initiator", lambda: object())

    def _process(event, *, store, resolve_agent_id, initiator):
        assert event.email_id == "msg_1"
        assert resolve_agent_id("maya.chen@agents.dev.bytedesk.ai") == "ag_maya"
        return inbox.AgenticInboxProcessResult(
            inbox.AgenticInboxEventStatus.DISPATCHED,
            event.event_id,
            agent_id="ag_maya",
            session_id="sess_1",
        )

    monkeypatch.setattr(inbox, "process_email_event", _process)
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: object())
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: object())

    body = json.dumps(
        {
            "event_id": "evt_1",
            "event_type": "email.received",
            "mailbox_id": "maya.chen@agents.dev.bytedesk.ai",
            "email_id": "msg_1",
        }
    ).encode()
    timestamp = str(int(time.time()))
    response = _client().post(
        "/v1/agentic-inbox/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Omnigent-Timestamp": timestamp,
            "X-Omnigent-Signature": _sign(body, "secret", timestamp),
        },
    )

    assert response.status_code == 202
    assert response.json()["session_id"] == "sess_1"


def test_agentic_inbox_route_unconfigured_secret_returns_404(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", raising=False)
    response = _client().post("/v1/agentic-inbox/events", json={"event_id": "evt_1"})
    assert response.status_code == 404
    assert response.json()["status"] == "unconfigured"


def test_agentic_inbox_route_rejects_invalid_payload(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    body = b"[]"
    timestamp = str(int(time.time()))
    response = _client().post(
        "/v1/agentic-inbox/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Omnigent-Timestamp": timestamp,
            "X-Omnigent-Signature": _sign(body, "secret", timestamp),
        },
    )
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_payload"


def test_agentic_inbox_route_dispatch_unavailable_without_initiator(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(sessions, "get_session_initiator", lambda: None)
    monkeypatch.setattr(sessions, "build_self_call_initiator_from_env", lambda: None)

    body = json.dumps(
        {
            "event_id": "evt_1",
            "event_type": "email.received",
            "mailbox_id": "maya@agents.dev.bytedesk.ai",
            "email_id": "msg_1",
        }
    ).encode()
    timestamp = str(int(time.time()))
    response = _client().post(
        "/v1/agentic-inbox/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Omnigent-Timestamp": timestamp,
            "X-Omnigent-Signature": _sign(body, "secret", timestamp),
        },
    )
    assert response.status_code == 503
    assert response.json()["status"] == "dispatch_unavailable"


def test_agentic_inbox_route_builds_initiator_from_env_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    built = object()
    set_calls: list[object] = []

    monkeypatch.setattr(sessions, "get_session_initiator", lambda: None)
    monkeypatch.setattr(sessions, "build_self_call_initiator_from_env", lambda: built)
    monkeypatch.setattr(
        sessions,
        "set_session_initiator",
        lambda initiator: set_calls.append(initiator),
    )

    class _Resolver:
        def __init__(self, *_args) -> None:
            pass

        def resolve_agent_id(self, _mailbox_id: str) -> str:
            return "ag_maya"

    monkeypatch.setattr(inbox, "AgenticInboxResolver", _Resolver)
    monkeypatch.setattr(inbox, "get_agentic_inbox_event_store", lambda: object())
    monkeypatch.setattr(
        inbox,
        "process_email_event",
        lambda *_a, **_k: inbox.AgenticInboxProcessResult(
            inbox.AgenticInboxEventStatus.DUPLICATE,
            "evt_1",
        ),
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: object())
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: object())

    body = json.dumps(
        {
            "event_id": "evt_1",
            "event_type": "email.received",
            "mailbox_id": "maya@agents.dev.bytedesk.ai",
            "email_id": "msg_1",
        }
    ).encode()
    timestamp = str(int(time.time()))
    response = _client().post(
        "/v1/agentic-inbox/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Omnigent-Timestamp": timestamp,
            "X-Omnigent-Signature": _sign(body, "secret", timestamp),
        },
    )
    assert response.status_code == 200
    assert set_calls == [built]


# ── ADR-0155 cutover flag (BDP-2567): default OFF = no behavior change ──────────


async def _async_true(*_args, **_kwargs) -> bool:
    return True


def _boom_ingest(**_kwargs):  # pragma: no cover - only invoked on regression
    raise AssertionError("ingest() must not run while the cutover flag is OFF")


def _signed_email_post(client: TestClient):
    body = json.dumps(
        {
            "event_id": "evt_1",
            "event_type": "email.received",
            "mailbox_id": "maya.chen@agents.dev.bytedesk.ai",
            "email_id": "msg_1",
        }
    ).encode()
    timestamp = str(int(time.time()))
    return client.post(
        "/v1/agentic-inbox/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Omnigent-Timestamp": timestamp,
            "X-Omnigent-Signature": _sign(body, "secret", timestamp),
        },
    )


def test_flag_off_by_default_never_calls_pipeline(monkeypatch) -> None:
    """Default flag state (unset → off) keeps the legacy dispatch path intact."""
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    class _Resolver:
        def __init__(self, *_args) -> None:
            pass

        def resolve_agent_id(self, _mailbox_id: str) -> str:
            return "ag_maya"

    monkeypatch.setattr(inbox, "AgenticInboxResolver", _Resolver)
    monkeypatch.setattr(inbox, "get_agentic_inbox_event_store", lambda: object())
    monkeypatch.setattr(sessions, "get_session_initiator", lambda: object())
    monkeypatch.setattr(
        inbox,
        "process_email_event",
        lambda *_a, **_k: inbox.AgenticInboxProcessResult(
            inbox.AgenticInboxEventStatus.DISPATCHED, "evt_1", agent_id="ag_maya",
            session_id="sess_1",
        ),
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: object())
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: object())

    response = _signed_email_post(_client())
    assert response.status_code == 202
    assert response.json()["session_id"] == "sess_1"


def test_flag_on_routes_through_pipeline(monkeypatch) -> None:
    """Flag ON → the verified email payload is handed to ``ingest`` on the inbox channel."""
    from bytedesk_omnigent.inbound.pipeline import IngestResult

    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true)

    calls: dict = {}

    def _fake_ingest(**kwargs):
        calls.update(kwargs)
        return IngestResult(
            status="projected", http_status=202, idempotency_key="agentic-inbox:evt_1",
            event_type="email.received",
        )

    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _fake_ingest)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.store.get_inbound_event_store", lambda: object()
    )
    monkeypatch.setattr("bytedesk_omnigent.inbound.processors.all_processors", list)

    response = _signed_email_post(_client())

    assert response.status_code == 202
    assert response.json()["status"] == "projected"
    assert response.json()["idempotencyKey"] == "agentic-inbox:evt_1"
    assert calls["channel"] == "agentic-inbox"
    assert calls["source"] == "agentic-inbox"
    assert calls["raw_payload"]["email_id"] == "msg_1"


def test_flag_on_bad_signature_still_rejected_before_pipeline(monkeypatch) -> None:
    """Signature verification stays the auth boundary before the pipeline runs."""
    monkeypatch.setenv("OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr("bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true)
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    response = _client().post(
        "/v1/agentic-inbox/events",
        json={"event_id": "evt_1"},
        headers={
            "X-Omnigent-Timestamp": str(int(time.time())),
            "X-Omnigent-Signature": "bad",
        },
    )
    assert response.status_code == 401
    assert response.json()["status"] == "bad_signature"
