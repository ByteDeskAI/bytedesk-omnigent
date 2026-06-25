"""Agentic Inbox email event trigger support (BDP-2455)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.db_models import SqlAgenticInboxEvent
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Agent
from omnigent.runtime.agent_cache import AgentCache
from omnigent.stores import AgentStore

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_ENV = "OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET"
WEBHOOK_EVENT_TYPE = "email.received"
WEBHOOK_SOURCE = "agentic-inbox"
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300


class AgenticInboxEventStatus(str, Enum):
    """Process outcome for an Agentic Inbox email event."""

    RECEIVED = "received"
    DISPATCHED = "dispatched"
    DEAD_LETTERED = "dead_lettered"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class AgenticInboxEmailEvent:
    """The signed Agentic Inbox event payload."""

    event_id: str
    event_type: str
    mailbox_id: str
    email_id: str
    message_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    thread_id: str | None = None
    received_at: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AgenticInboxEmailEvent:
        """Validate and normalize the webhook payload."""
        event_id = str(payload.get("event_id") or "").strip()
        event_type = str(payload.get("event_type") or "").strip()
        mailbox_id = str(payload.get("mailbox_id") or "").strip().lower()
        email_id = str(payload.get("email_id") or "").strip()
        if not event_id:
            raise ValueError("event_id is required")
        if event_type != WEBHOOK_EVENT_TYPE:
            raise ValueError(f"unsupported event_type {event_type!r}")
        if not mailbox_id:
            raise ValueError("mailbox_id is required")
        if not email_id:
            raise ValueError("email_id is required")
        return cls(
            event_id=event_id,
            event_type=event_type,
            mailbox_id=mailbox_id,
            email_id=email_id,
            message_id=_optional_str(payload.get("message_id")),
            sender=_optional_str(payload.get("sender")),
            subject=_optional_str(payload.get("subject")),
            thread_id=_optional_str(payload.get("thread_id")),
            received_at=_optional_str(payload.get("received_at")),
        )

    def payload_json(self) -> str:
        """Return a compact JSON snapshot of the event metadata."""
        return json.dumps(
            {
                "event_id": self.event_id,
                "event_type": self.event_type,
                "mailbox_id": self.mailbox_id,
                "email_id": self.email_id,
                "message_id": self.message_id,
                "sender": self.sender,
                "subject": self.subject,
                "thread_id": self.thread_id,
                "received_at": self.received_at,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class AgenticInboxEventRecord:
    """A persisted Agentic Inbox event row."""

    event_id: str
    event_type: str
    mailbox_id: str
    email_id: str
    status: str
    message_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    thread_id: str | None = None
    received_at: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    attempts: int = 0
    error: str | None = None


@dataclass(frozen=True)
class AgenticInboxProcessResult:
    """Result returned by ``process_email_event``."""

    status: AgenticInboxEventStatus
    event_id: str
    agent_id: str | None = None
    session_id: str | None = None
    detail: str | None = None


@runtime_checkable
class AgenticInboxEventStore(Protocol):
    """Persistent Agentic Inbox event status store."""

    def record_received(
        self, event: AgenticInboxEmailEvent, *, now: int | None = None
    ) -> tuple[AgenticInboxEventRecord, bool]:
        """Record an event if absent; returns ``(record, inserted)``."""
        ...

    def mark_dispatched(
        self,
        event_id: str,
        *,
        agent_id: str,
        session_id: str,
        now: int | None = None,
    ) -> AgenticInboxEventRecord:
        """Mark an event as dispatched."""
        ...

    def mark_dead_lettered(
        self, event_id: str, *, error: str, now: int | None = None
    ) -> AgenticInboxEventRecord:
        """Mark an event as permanently undeliverable."""
        ...

    def mark_failed(
        self, event_id: str, *, error: str, now: int | None = None
    ) -> AgenticInboxEventRecord:
        """Mark an event dispatch attempt as failed."""
        ...

    def get(self, event_id: str) -> AgenticInboxEventRecord | None:
        """Return the stored event, if present."""
        ...


class AgenticInboxResolutionError(RuntimeError):
    """Raised when a mailbox maps ambiguously or unsafely."""


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _header(headers: Mapping[str, str], name: str) -> str:
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def verify_agentic_inbox_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """Verify the Agentic Inbox HMAC signature.

    The signed material is ``<timestamp>.<raw_body>``. The timestamp window keeps
    a captured delivery from being replayed indefinitely even if the body and
    signature are known.
    """
    timestamp = _header(headers, "x-omnigent-timestamp")
    signature = _header(headers, "x-omnigent-signature")
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = int(time.time()) if now is None else now
    if abs(now - ts) > tolerance_seconds:
        return False
    signed = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    provided = signature.split("=", 1)[1] if "=" in signature else signature
    return hmac.compare_digest(expected, provided)


def _to_record(row: SqlAgenticInboxEvent) -> AgenticInboxEventRecord:
    return AgenticInboxEventRecord(
        event_id=row.event_id,
        event_type=row.event_type,
        mailbox_id=row.mailbox_id,
        email_id=row.email_id,
        message_id=row.message_id,
        sender=row.sender,
        subject=row.subject,
        thread_id=row.thread_id,
        received_at=row.received_at,
        agent_id=row.agent_id,
        session_id=row.session_id,
        status=row.status,
        attempts=row.attempts,
        error=row.error,
    )


class SqlAlchemyAgenticInboxEventStore:
    """SQLAlchemy-backed Agentic Inbox event store."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        """The underlying SQLAlchemy engine."""
        return self._engine

    def record_received(
        self, event: AgenticInboxEmailEvent, *, now: int | None = None
    ) -> tuple[AgenticInboxEventRecord, bool]:
        now = now_epoch() if now is None else now
        try:
            with self._write_session() as session:
                row = SqlAgenticInboxEvent(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    mailbox_id=event.mailbox_id,
                    email_id=event.email_id,
                    message_id=event.message_id,
                    sender=event.sender,
                    subject=event.subject,
                    thread_id=event.thread_id,
                    received_at=event.received_at,
                    status=AgenticInboxEventStatus.RECEIVED.value,
                    payload=event.payload_json(),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return _to_record(row), True
        except IntegrityError:
            existing = self.get(event.event_id)
            if existing is None:
                raise
            return existing, False

    def mark_dispatched(
        self,
        event_id: str,
        *,
        agent_id: str,
        session_id: str,
        now: int | None = None,
    ) -> AgenticInboxEventRecord:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlAgenticInboxEvent, event_id)
            if row is None:
                raise KeyError(event_id)
            row.agent_id = agent_id
            row.session_id = session_id
            row.status = AgenticInboxEventStatus.DISPATCHED.value
            row.error = None
            row.attempts += 1
            row.dispatched_at = now
            row.updated_at = now
            session.flush()
            return _to_record(row)

    def mark_dead_lettered(
        self, event_id: str, *, error: str, now: int | None = None
    ) -> AgenticInboxEventRecord:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlAgenticInboxEvent, event_id)
            if row is None:
                raise KeyError(event_id)
            row.status = AgenticInboxEventStatus.DEAD_LETTERED.value
            row.error = error
            row.updated_at = now
            session.flush()
            return _to_record(row)

    def mark_failed(
        self, event_id: str, *, error: str, now: int | None = None
    ) -> AgenticInboxEventRecord:
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.get(SqlAgenticInboxEvent, event_id)
            if row is None:
                raise KeyError(event_id)
            row.status = AgenticInboxEventStatus.FAILED.value
            row.error = error
            row.attempts += 1
            row.updated_at = now
            session.flush()
            return _to_record(row)

    def get(self, event_id: str) -> AgenticInboxEventRecord | None:
        with self._session() as session:
            row = session.get(SqlAgenticInboxEvent, event_id)
            return _to_record(row) if row is not None else None


class AgenticInboxResolver:
    """Resolve Agentic Inbox mailbox addresses to persona template agents."""

    def __init__(self, agent_store: AgentStore, agent_cache: AgentCache) -> None:
        self._agent_store = agent_store
        self._agent_cache = agent_cache

    def resolve_agent_id(self, mailbox_id: str) -> str | None:
        mailbox = mailbox_id.strip().lower()
        candidates: list[Agent] = []
        after: str | None = None
        while True:
            page = self._agent_store.list(limit=1000, after=after, order="asc")
            for agent in page.data:
                if agent.session_id is not None:
                    continue
                try:
                    loaded = self._agent_cache.load(
                        agent.id,
                        agent.bundle_location,
                        expand_env=True,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad bundle must not block mail
                    logger.warning("agentic inbox resolver skipped %s: %s", agent.id, exc)
                    continue
                params = loaded.spec.params or {}
                if _truthy(params.get("workflow")):
                    continue
                if not _optional_str(params.get("displayName")):
                    continue
                addresses = {
                    _optional_str(params.get("mailboxId")),
                    _optional_str(params.get("email")),
                }
                if mailbox in {a.lower() for a in addresses if a}:
                    candidates.append(agent)
            if not page.has_more:
                break
            after = page.last_id
        if not candidates:
            return None
        if len(candidates) > 1:
            ids = ", ".join(a.id for a in candidates)
            raise AgenticInboxResolutionError(f"mailbox {mailbox} maps to multiple agents: {ids}")
        return candidates[0].id


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def build_email_received_prompt(event: AgenticInboxEmailEvent) -> str:
    """Build the deterministic user message that wakes the persona agent."""
    subject = event.subject or "(no subject)"
    sender = event.sender or "(unknown sender)"
    thread = f" Thread ID: {event.thread_id}." if event.thread_id else ""
    return (
        f"You received a new email in your mailbox {event.mailbox_id}. "
        f"Use the agentic-inbox MCP tools for mailbox {event.mailbox_id} to read "
        f"email {event.email_id}, assess whether action is needed, and draft a "
        f"response if appropriate. Do not send email unless your policy or task "
        f"context authorizes it. Sender: {sender}. Subject: {subject}.{thread}"
    )


def process_email_event(
    event: AgenticInboxEmailEvent,
    *,
    store: AgenticInboxEventStore,
    resolve_agent_id: Callable[[str], str | None],
    initiator,
    now: int | None = None,
) -> AgenticInboxProcessResult:
    """Record and dispatch an Agentic Inbox email event."""
    record, inserted = store.record_received(event, now=now)
    if not inserted and record.status == AgenticInboxEventStatus.DISPATCHED.value:
        return AgenticInboxProcessResult(
            AgenticInboxEventStatus.DUPLICATE,
            event.event_id,
            agent_id=record.agent_id,
            session_id=record.session_id,
            detail="event already dispatched",
        )
    if not inserted and record.status == AgenticInboxEventStatus.DEAD_LETTERED.value:
        return AgenticInboxProcessResult(
            AgenticInboxEventStatus.DUPLICATE,
            event.event_id,
            agent_id=record.agent_id,
            session_id=record.session_id,
            detail="event already dead-lettered",
        )

    try:
        agent_id = resolve_agent_id(event.mailbox_id)
    except AgenticInboxResolutionError as exc:
        detail = str(exc)
        store.mark_dead_lettered(event.event_id, error=detail, now=now)
        return AgenticInboxProcessResult(
            AgenticInboxEventStatus.DEAD_LETTERED,
            event.event_id,
            detail=detail,
        )
    if not agent_id:
        detail = f"no persona agent mapped to mailbox {event.mailbox_id}"
        store.mark_dead_lettered(event.event_id, error=detail, now=now)
        return AgenticInboxProcessResult(
            AgenticInboxEventStatus.DEAD_LETTERED,
            event.event_id,
            detail=detail,
        )

    metadata = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "mailbox_id": event.mailbox_id,
        "email_id": event.email_id,
    }
    if event.message_id:
        metadata["message_id"] = event.message_id
    if event.thread_id:
        metadata["thread_id"] = event.thread_id
    external_key = f"agentic-inbox:{event.event_id}"
    try:
        session_id = initiator.initiate(
            agent_id=agent_id,
            prompt=build_email_received_prompt(event),
            source=f"{WEBHOOK_SOURCE}:{event.event_type}",
            metadata=metadata,
            external_key=external_key,
        )
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        detail = f"dispatch failed: {exc}"
        store.mark_failed(event.event_id, error=detail, now=now)
        return AgenticInboxProcessResult(
            AgenticInboxEventStatus.FAILED,
            event.event_id,
            agent_id=agent_id,
            detail=detail,
        )

    store.mark_dispatched(event.event_id, agent_id=agent_id, session_id=session_id, now=now)
    return AgenticInboxProcessResult(
        AgenticInboxEventStatus.DISPATCHED,
        event.event_id,
        agent_id=agent_id,
        session_id=session_id,
    )


_event_store_cache: dict[str, AgenticInboxEventStore] = {}


def get_agentic_inbox_event_store() -> AgenticInboxEventStore:
    """Return the durable Agentic Inbox event store for the active DB."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _event_store_cache.get(location)
    if store is None:
        store = SqlAlchemyAgenticInboxEventStore(location)
        _event_store_cache[location] = store
    return store


__all__ = [
    "WEBHOOK_SECRET_ENV",
    "AgenticInboxEmailEvent",
    "AgenticInboxEventStatus",
    "AgenticInboxProcessResult",
    "AgenticInboxResolver",
    "SqlAlchemyAgenticInboxEventStore",
    "build_email_received_prompt",
    "get_agentic_inbox_event_store",
    "process_email_event",
    "verify_agentic_inbox_signature",
]
