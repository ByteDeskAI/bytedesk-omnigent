"""Per-channel Message Translators (ADR-0155, BDP-2558).

**Message Translator / Normalizer** (EIP) implemented as an **Adapter** (GoF): turn
a raw inbound webhook/payload into the canonical :class:`InboundEvent`. Each
translator **wraps the existing, already-tested parse function** — no parsing is
rewritten here.

Translators are keyed by **channel** (the logical inbound channel a route serves),
not by bare source: the same source ``github`` means a goal-delivery PR-merge on one
route and a signal-bus deliver on another, so the route picks the channel and the
translator branches on source internally. A :class:`PluggableRegistry` resolves the
channel translator; an unknown channel or a non-actionable payload yields ``None``
(the route acks "ignored").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from omnigent.kernel.pluggable import PluggableRegistry

from bytedesk_omnigent.inbound.event import (
    InboundEvent,
    body_fingerprint,
    select_headers,
)

# Channel names (1:1 with the Channel-Adapter routes that own them).
CHANNEL_GOAL_DELIVERY = "goal-delivery"
CHANNEL_SIGNAL = "signal"
CHANNEL_AGENTIC_INBOX = "agentic-inbox"

_GITHUB_HEADERS = ("x-github-event", "x-github-delivery", "x-omnigent-event")
_JIRA_HEADERS = ("x-atlassian-webhook-identifier",)


@runtime_checkable
class InboundTranslator(Protocol):
    """Translate a raw inbound payload into a canonical event (or ``None`` to ignore)."""

    def translate(
        self, *, source: str, raw_payload: dict, headers: Mapping[str, str], now: int
    ) -> InboundEvent | None: ...


class GoalDeliveryTranslator:
    """Goal-delivery channel: GitHub PR-merged + Jira issue-updated (ADR-0154)."""

    def translate(self, *, source, raw_payload, headers, now):
        if source == "github":
            return self._github(raw_payload, headers, now)
        if source == "jira":
            return self._jira(raw_payload, headers, now)
        return None

    def _github(self, raw_payload, headers, now):
        from bytedesk_omnigent.goals_delivery import parse_github_pr_event

        event = parse_github_pr_event(raw_payload)
        if event is None:
            return None
        delivery = select_headers(headers, ("x-github-delivery",)).get("x-github-delivery")
        # Stable key: GitHub redelivers with the same X-GitHub-Delivery guid; fall back
        # to the merge commit sha, then a body fingerprint.
        token = delivery or event.merge_commit_sha or body_fingerprint(raw_payload)
        return InboundEvent(
            idempotency_key=f"github:pr:{event.repo}#{event.pr_number}:{token}",
            source="github",
            type="pull_request.merged",
            occurred_at=now,
            received_at=now,
            raw_payload=raw_payload,
            normalized={
                "repo": event.repo,
                "prNumber": event.pr_number,
                "headRef": event.head_ref,
                "baseRef": event.base_ref,
                "mergeCommitSha": event.merge_commit_sha,
            },
            headers=select_headers(headers, _GITHUB_HEADERS),
            event_id=delivery,
        )

    def _jira(self, raw_payload, headers, now):
        from bytedesk_omnigent.goals_delivery import parse_jira_issue_event

        wid = select_headers(headers, _JIRA_HEADERS).get("x-atlassian-webhook-identifier")
        event = parse_jira_issue_event(raw_payload, webhook_identifier=wid)
        if event is None:
            return None
        # Jira has no reliable per-event delivery id (the webhook-identifier is per
        # webhook config, not per event), so the key includes statusCategory — a real
        # re-transition to a DIFFERENT status is a distinct event, a redelivery of the
        # same one dedupes. The Wire-Tap log remains the source of truth for "seen".
        token = wid or body_fingerprint(raw_payload)
        return InboundEvent(
            idempotency_key=f"jira:{event.issue_key}:{event.status_category}:{token}",
            source="jira",
            type="jira.issue_updated",
            occurred_at=now,
            received_at=now,
            raw_payload=raw_payload,
            normalized={
                "issueKey": event.issue_key,
                "issueType": event.issue_type,
                "status": event.status,
                "statusCategory": event.status_category,
                "parentEpicKey": event.parent_epic_key,
            },
            headers=select_headers(headers, _JIRA_HEADERS),
            event_id=wid,
        )


class AgenticInboxTranslator:
    """Agentic-inbox channel: inbound email events (BDP-2171)."""

    def translate(self, *, source, raw_payload, headers, now):
        from bytedesk_omnigent.agentic_inbox import AgenticInboxEmailEvent

        try:
            event = AgenticInboxEmailEvent.from_payload(raw_payload)
        except (ValueError, KeyError, TypeError):
            return None
        return InboundEvent(
            idempotency_key=f"agentic-inbox:{event.event_id}",
            source="agentic-inbox",
            type="email.received",
            occurred_at=now,  # source received_at is an ISO string, not epoch — keep it in normalized
            received_at=now,
            raw_payload=raw_payload,
            normalized={
                "eventId": event.event_id,
                "mailboxId": event.mailbox_id,
                "emailId": event.email_id,
                "subject": event.subject,
                "sender": event.sender,
                "threadId": event.thread_id,
                "receivedAt": event.received_at,
            },
            headers={},
            event_id=event.event_id,
        )


class SignalTranslator:
    """Signal-bus ingress channel: deliver a signal to a parked session (ADR-0142).

    The ingress route has no typed event — it resolves a binding from the adapter's
    ``match_key`` and delivers the raw body. The canonical event captures that match
    key; the idempotency key is ``signal:{source}:{match_key}:{body-hash}`` (the
    signal bus's own AlreadyResolved is a second dedupe layer behind this).
    """

    def __init__(self, match_key_for: Callable[[str, Mapping[str, str]], str] | None = None) -> None:
        self._match_key_for = match_key_for

    def translate(self, *, source, raw_payload, headers, now):
        match_key = self._resolve_match_key(source, headers)
        return InboundEvent(
            idempotency_key=f"signal:{source}:{match_key}:{body_fingerprint(raw_payload)}",
            source=source,
            type="signal.deliver",
            occurred_at=now,
            received_at=now,
            raw_payload=raw_payload,
            normalized={"matchKey": match_key},
            headers=select_headers(headers, ("x-omnigent-event",)),
        )

    def _resolve_match_key(self, source, headers):
        if self._match_key_for is not None:
            return self._match_key_for(source, headers)
        from bytedesk_omnigent.ingress import resolve_webhook_adapter

        return resolve_webhook_adapter(source).match_key(headers)


def _build_translator_registry() -> PluggableRegistry[InboundTranslator]:
    """The per-channel translator registry (no default — unknown channel → None)."""
    registry: PluggableRegistry[InboundTranslator] = PluggableRegistry("inbound_translator")
    registry.register(CHANNEL_GOAL_DELIVERY, GoalDeliveryTranslator)
    registry.register(CHANNEL_AGENTIC_INBOX, AgenticInboxTranslator)
    registry.register(CHANNEL_SIGNAL, SignalTranslator)
    return registry


_translator_registry: PluggableRegistry[InboundTranslator] | None = None


def register_translator(channel: str, factory: Callable[[], InboundTranslator]) -> None:
    """Register a per-channel translator *factory* (idempotent)."""
    global _translator_registry
    if _translator_registry is None:
        _translator_registry = _build_translator_registry()
    if channel not in _translator_registry.names():
        _translator_registry.register(channel, factory)


def resolve_translator(channel: str) -> InboundTranslator | None:
    """Resolve the translator for *channel*, or ``None`` when none is registered."""
    global _translator_registry
    if _translator_registry is None:
        _translator_registry = _build_translator_registry()
    if channel in _translator_registry.names():
        return _translator_registry.get(channel)
    return None
