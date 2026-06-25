"""Agentic Inbox email event trigger tests (BDP-2455).

Inbound mail is stored by the Cloudflare Worker, then Omnigent receives a signed
``email.received`` event and starts the matching persona agent through the normal
SessionInitiator seam. These tests pin the pure contract: authentication,
mailbox-to-persona resolution, idempotent dispatch, and dead-letter status.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from bytedesk_omnigent.agentic_inbox import (
    AgenticInboxEmailEvent,
    AgenticInboxEventStatus,
    AgenticInboxResolver,
    SqlAlchemyAgenticInboxEventStore,
    process_email_event,
    verify_agentic_inbox_signature,
)
from omnigent.entities import Agent, LoadedAgent, PagedList
from omnigent.spec.types import AgentSpec


def _signature(raw_body: bytes, secret: str, timestamp: str) -> str:
    signed = timestamp.encode("utf-8") + b"." + raw_body
    return "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


@dataclass
class _Loaded:
    spec: AgentSpec


class _AgentStore:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = agents

    def list(self, limit=20, after=None, before=None, order="desc") -> PagedList[Agent]:
        return PagedList(
            data=self._agents[:limit],
            first_id=self._agents[0].id if self._agents else None,
            last_id=self._agents[-1].id if self._agents else None,
            has_more=False,
        )


class _AgentCache:
    def __init__(self, specs: dict[str, AgentSpec]) -> None:
        self._specs = specs

    def load(self, agent_id: str, bundle_location: str, *, expand_env=False) -> LoadedAgent:
        return LoadedAgent(spec=self._specs[agent_id], workdir=Path("."))


class _RecordingInitiator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def initiate(
        self,
        *,
        agent_id: str,
        prompt: str,
        source: str,
        metadata: dict | None = None,
        external_key: str | None = None,
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


def _agent(agent_id: str, name: str) -> Agent:
    return Agent(
        id=agent_id,
        created_at=1,
        name=name,
        bundle_location=f"{agent_id}/bundle",
    )


def _event(**overrides: object) -> AgenticInboxEmailEvent:
    data = {
        "event_id": "mail_maya_msg_1",
        "event_type": "email.received",
        "mailbox_id": "maya.chen@agents.dev.bytedesk.ai",
        "email_id": "msg_1",
        "message_id": "rfc-message-1",
        "sender": "customer@example.com",
        "subject": "Need help",
        "thread_id": "thread_1",
        "received_at": "2026-06-24T12:00:00Z",
    }
    data.update(overrides)
    return AgenticInboxEmailEvent(**data)


def test_verify_agentic_inbox_signature_uses_timestamped_body() -> None:
    raw = json.dumps({"event_id": "mail_1"}).encode("utf-8")
    secret = "dev-secret"
    timestamp = "1782297600"
    headers = {
        "x-omnigent-timestamp": timestamp,
        "x-omnigent-signature": _signature(raw, secret, timestamp),
    }

    assert verify_agentic_inbox_signature(raw, headers, secret, now=1782297600) is True
    bad_headers = {**headers, "x-omnigent-signature": "bad"}
    assert verify_agentic_inbox_signature(raw, bad_headers, secret, now=1782297600) is False
    assert verify_agentic_inbox_signature(raw, headers, secret, now=1782298201) is False


def test_resolver_matches_persona_by_mailbox_and_ignores_workflows() -> None:
    agents = [_agent("ag_workflow", "workflow"), _agent("ag_maya", "chief-of-staff")]
    specs = {
        "ag_workflow": AgentSpec(
            spec_version=1,
            name="workflow",
            params={"workflow": True, "mailboxId": "maya.chen@agents.dev.bytedesk.ai"},
        ),
        "ag_maya": AgentSpec(
            spec_version=1,
            name="chief-of-staff",
            params={
                "displayName": "Maya Chen",
                "mailboxId": "maya.chen@agents.dev.bytedesk.ai",
            },
        ),
    }

    resolver = AgenticInboxResolver(_AgentStore(agents), _AgentCache(specs))

    assert resolver.resolve_agent_id("maya.chen@agents.dev.bytedesk.ai") == "ag_maya"


def test_process_email_event_dispatches_once_with_external_key(tmp_path) -> None:
    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    initiator = _RecordingInitiator()

    def resolver(mailbox_id: str) -> str:
        return "ag_maya"

    first = process_email_event(
        _event(), store=store, resolve_agent_id=resolver, initiator=initiator
    )
    second = process_email_event(
        _event(), store=store, resolve_agent_id=resolver, initiator=initiator
    )

    assert first.status is AgenticInboxEventStatus.DISPATCHED
    assert first.session_id == "sess_1"
    assert second.status is AgenticInboxEventStatus.DUPLICATE
    assert len(initiator.calls) == 1
    call = initiator.calls[0]
    assert call["agent_id"] == "ag_maya"
    assert call["source"] == "agentic-inbox:email.received"
    assert call["external_key"] == "agentic-inbox:mail_maya_msg_1"
    assert call["metadata"]["mailbox_id"] == "maya.chen@agents.dev.bytedesk.ai"
    assert "read email msg_1" in call["prompt"]

    record = store.get("mail_maya_msg_1")
    assert record is not None
    assert record.status == "dispatched"
    assert record.agent_id == "ag_maya"
    assert record.session_id == "sess_1"


def test_process_email_event_dead_letters_unknown_mailbox(tmp_path) -> None:
    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    initiator = _RecordingInitiator()

    result = process_email_event(
        _event(mailbox_id="unknown@agents.dev.bytedesk.ai"),
        store=store,
        resolve_agent_id=lambda mailbox_id: None,
        initiator=initiator,
    )

    assert result.status is AgenticInboxEventStatus.DEAD_LETTERED
    assert result.detail == "no persona agent mapped to mailbox unknown@agents.dev.bytedesk.ai"
    assert initiator.calls == []
    record = store.get("mail_maya_msg_1")
    assert record is not None
    assert record.status == "dead_lettered"
    assert record.error == result.detail


def test_from_payload_validates_required_fields() -> None:
    base = {
        "event_type": "email.received",
        "mailbox_id": "maya@agents.dev.bytedesk.ai",
        "email_id": "msg_1",
    }
    with pytest.raises(ValueError, match="event_id"):
        AgenticInboxEmailEvent.from_payload({**base, "event_id": ""})
    with pytest.raises(ValueError, match="event_type"):
        AgenticInboxEmailEvent.from_payload({**base, "event_id": "e1", "event_type": "bad"})
    with pytest.raises(ValueError, match="mailbox_id"):
        AgenticInboxEmailEvent.from_payload({**base, "event_id": "e1", "mailbox_id": ""})
    with pytest.raises(ValueError, match="email_id"):
        AgenticInboxEmailEvent.from_payload({**base, "event_id": "e1", "email_id": ""})


def test_from_payload_normalizes_mailbox_and_payload_json() -> None:
    event = AgenticInboxEmailEvent.from_payload(
        {
            "event_id": "evt_1",
            "event_type": "email.received",
            "mailbox_id": "Maya@Agents.Dev.Bytedesk.AI",
            "email_id": "msg_1",
            "sender": "  a@b.com ",
            "subject": "",
        }
    )
    assert event.mailbox_id == "maya@agents.dev.bytedesk.ai"
    assert event.sender == "a@b.com"
    assert event.subject is None
    assert '"mailbox_id"' in event.payload_json()


def test_verify_signature_rejects_missing_and_invalid_timestamp() -> None:
    raw = b"{}"
    secret = "s"
    assert verify_agentic_inbox_signature(raw, {}, secret) is False
    assert (
        verify_agentic_inbox_signature(
            raw,
            {"x-omnigent-timestamp": "not-int", "x-omnigent-signature": "sha256=abc"},
            secret,
        )
        is False
    )
    headers = {
        "X-Omnigent-Timestamp": "1782297600",
        "x-omnigent-signature": _signature(raw, secret, "1782297600"),
    }
    assert verify_agentic_inbox_signature(raw, headers, secret, now=1782297600) is True


def test_sql_store_round_trip_and_mark_paths(tmp_path) -> None:
    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    assert store.engine is not None
    event = _event(event_id="evt_store")
    record, inserted = store.record_received(event, now=100)
    assert inserted is True
    assert record.status == "received"

    again, dup = store.record_received(event, now=101)
    assert dup is False
    assert again.event_id == "evt_store"

    dispatched = store.mark_dispatched(
        "evt_store", agent_id="ag_maya", session_id="sess_1", now=102
    )
    assert dispatched.status == "dispatched"
    assert dispatched.agent_id == "ag_maya"

    failed = store.mark_failed("evt_store", error="boom", now=103)
    assert failed.status == "failed"
    assert failed.error == "boom"
    assert failed.attempts == 2

    dead = store.mark_dead_lettered("evt_store", error="final", now=104)
    assert dead.status == "dead_lettered"

    with pytest.raises(KeyError):
        store.mark_dispatched("missing", agent_id="ag", session_id="s")
    with pytest.raises(KeyError):
        store.mark_dead_lettered("missing", error="gone")
    with pytest.raises(KeyError):
        store.mark_failed("missing", error="gone")


def test_process_email_event_duplicate_dead_lettered_and_failed(tmp_path) -> None:
    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    initiator = _RecordingInitiator()

    dead = process_email_event(
        _event(event_id="evt_dead"),
        store=store,
        resolve_agent_id=lambda _m: None,
        initiator=initiator,
    )
    assert dead.status is AgenticInboxEventStatus.DEAD_LETTERED

    dup = process_email_event(
        _event(event_id="evt_dead"),
        store=store,
        resolve_agent_id=lambda _m: "ag_maya",
        initiator=initiator,
    )
    assert dup.status is AgenticInboxEventStatus.DUPLICATE
    assert dup.detail == "event already dead-lettered"

    class _BoomInitiator:
        def initiate(self, **_kwargs: object) -> str:
            raise RuntimeError("server down")

    fail = process_email_event(
        _event(event_id="evt_fail"),
        store=store,
        resolve_agent_id=lambda _m: "ag_maya",
        initiator=_BoomInitiator(),
    )
    assert fail.status is AgenticInboxEventStatus.FAILED
    assert "dispatch failed" in (fail.detail or "")


def test_process_email_event_dead_letters_resolution_error(tmp_path) -> None:
    from bytedesk_omnigent.agentic_inbox import AgenticInboxResolutionError

    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")

    def _ambiguous(_mailbox: str) -> str:
        raise AgenticInboxResolutionError("mailbox maps to multiple agents")

    result = process_email_event(
        _event(event_id="evt_ambig"),
        store=store,
        resolve_agent_id=_ambiguous,
        initiator=_RecordingInitiator(),
    )
    assert result.status is AgenticInboxEventStatus.DEAD_LETTERED
    assert "multiple agents" in (result.detail or "")


def test_resolver_skips_session_agents_and_bad_bundles() -> None:
    agents = [
        _agent("ag_session", "session-bound"),
        _agent("ag_bad", "bad-bundle"),
        _agent("ag_maya", "chief-of-staff"),
    ]
    specs = {
        "ag_session": AgentSpec(spec_version=1, name="s", params={"displayName": "S"}),
        "ag_bad": AgentSpec(spec_version=1, name="b", params={"displayName": "B"}),
        "ag_maya": AgentSpec(
            spec_version=1,
            name="maya",
            params={
                "displayName": "Maya Chen",
                "email": "maya.chen@agents.dev.bytedesk.ai",
            },
        ),
    }
    cache = _AgentCache(specs)

    def _load_fail(agent_id: str, *_a: object, **_k: object) -> LoadedAgent:
        if agent_id == "ag_bad":
            raise OSError("unreadable bundle")
        return LoadedAgent(spec=specs[agent_id], workdir=Path("."))

    cache.load = _load_fail  # type: ignore[method-assign]
    agents[0] = Agent(
        id="ag_session",
        created_at=1,
        name="session-bound",
        bundle_location="ag_session/bundle",
        session_id="conv_1",
    )
    resolver = AgenticInboxResolver(_AgentStore(agents), cache)
    assert resolver.resolve_agent_id("maya.chen@agents.dev.bytedesk.ai") == "ag_maya"


def test_resolver_raises_on_ambiguous_mailbox() -> None:
    from bytedesk_omnigent.agentic_inbox import AgenticInboxResolutionError

    agents = [_agent("ag_a", "a"), _agent("ag_b", "b")]
    specs = {
        "ag_a": AgentSpec(
            spec_version=1,
            name="a",
            params={"displayName": "A", "mailboxId": "same@agents.dev.bytedesk.ai"},
        ),
        "ag_b": AgentSpec(
            spec_version=1,
            name="b",
            params={"displayName": "B", "mailboxId": "same@agents.dev.bytedesk.ai"},
        ),
    }
    resolver = AgenticInboxResolver(_AgentStore(agents), _AgentCache(specs))
    with pytest.raises(AgenticInboxResolutionError, match="multiple agents"):
        resolver.resolve_agent_id("same@agents.dev.bytedesk.ai")


def test_resolver_paginates_agent_list() -> None:
    page1 = [_agent("ag_fill", "fill")]
    page2 = [_agent("ag_maya", "maya")]
    specs = {
        "ag_fill": AgentSpec(spec_version=1, name="fill"),
        "ag_maya": AgentSpec(
            spec_version=1,
            name="maya",
            params={
                "displayName": "Maya",
                "mailboxId": "maya@agents.dev.bytedesk.ai",
            },
        ),
    }

    class _PagingStore:
        def list(self, limit=1000, after=None, before=None, order="asc") -> PagedList[Agent]:
            if after is None:
                return PagedList(data=page1, first_id="ag_fill", last_id="ag_fill", has_more=True)
            return PagedList(data=page2, first_id="ag_maya", last_id="ag_maya", has_more=False)

    resolver = AgenticInboxResolver(_PagingStore(), _AgentCache(specs))
    assert resolver.resolve_agent_id("maya@agents.dev.bytedesk.ai") == "ag_maya"


def test_resolver_returns_none_when_no_mailbox_match() -> None:
    agents = [_agent("ag_other", "other")]
    specs = {
        "ag_other": AgentSpec(
            spec_version=1,
            name="other",
            params={"displayName": "Other", "mailboxId": "other@agents.dev.bytedesk.ai"},
        ),
    }
    resolver = AgenticInboxResolver(_AgentStore(agents), _AgentCache(specs))
    assert resolver.resolve_agent_id("missing@agents.dev.bytedesk.ai") is None


def test_resolver_truthy_workflow_param_skips_string_workflow_flag() -> None:
    agents = [_agent("ag_wf", "workflow-flag")]
    specs = {
        "ag_wf": AgentSpec(
            spec_version=1,
            name="workflow-flag",
            params={
                "displayName": "Workflow",
                "mailboxId": "wf@agents.dev.bytedesk.ai",
                "workflow": " yes ",
            },
        ),
    }
    resolver = AgenticInboxResolver(_AgentStore(agents), _AgentCache(specs))
    assert resolver.resolve_agent_id("wf@agents.dev.bytedesk.ai") is None


def test_record_received_reraises_integrity_when_existing_row_missing(
    monkeypatch, tmp_path
) -> None:
    from sqlalchemy.exc import IntegrityError

    store = SqlAlchemyAgenticInboxEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    event = _event(event_id="evt_race")
    monkeypatch.setattr(store, "get", lambda _event_id: None)

    class _FailingSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def add(self, _row: object) -> None:
            return None

        def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr(store, "_write_session", lambda: _FailingSession())

    with pytest.raises(IntegrityError):
        store.record_received(event)


def test_get_agentic_inbox_event_store_caches_by_location(monkeypatch, tmp_path) -> None:
    from bytedesk_omnigent import agentic_inbox as mod

    location = f"sqlite:///{tmp_path / 'shared.db'}"
    mod._event_store_cache.clear()

    class _ConvStore:
        storage_location = location

    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: _ConvStore())
    first = mod.get_agentic_inbox_event_store()
    second = mod.get_agentic_inbox_event_store()
    assert first is second
    mod._event_store_cache.clear()
