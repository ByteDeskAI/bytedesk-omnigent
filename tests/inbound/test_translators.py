"""Tests for the canonical InboundEvent + per-channel translators (ADR-0155, BDP-2558)."""
from __future__ import annotations

from bytedesk_omnigent.inbound.event import InboundEvent, body_fingerprint, select_headers
from bytedesk_omnigent.inbound.translators import (
    CHANNEL_AGENTIC_INBOX,
    CHANNEL_GOAL_DELIVERY,
    CHANNEL_SIGNAL,
    AgenticInboxTranslator,
    GoalDeliveryTranslator,
    SignalTranslator,
    resolve_translator,
)

_GH_MERGED = {
    "action": "closed",
    "pull_request": {"number": 987, "merged": True, "head": {"ref": "feature/x"},
                     "base": {"ref": "develop"}, "merge_commit_sha": "deadbeef"},
    "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
}
_JIRA_DONE = {
    "webhookEvent": "jira:issue_updated",
    "issue": {"key": "BDP-1235", "fields": {"issuetype": {"name": "Task"},
              "status": {"name": "Done", "statusCategory": {"key": "done"}},
              "parent": {"key": "BDP-1234"}}},
}
_EMAIL = {"event_id": "evt-1", "event_type": "email.received", "mailbox_id": "Maya@x.com",
          "email_id": "em-1", "subject": "hi", "sender": "a@b.com", "received_at": "2026-06-26T00:00:00Z"}


# -- canonical envelope helpers ----------------------------------------------
def test_select_headers_is_case_insensitive_and_drops_signature() -> None:
    h = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=secret"}
    picked = select_headers(h, ("x-github-event", "x-hub-signature-256"))
    # we deliberately never include the signature header in any translator whitelist
    assert picked["x-github-event"] == "pull_request"


def test_body_fingerprint_is_stable_and_order_independent() -> None:
    assert body_fingerprint({"a": 1, "b": 2}) == body_fingerprint({"b": 2, "a": 1})
    assert body_fingerprint({"a": 1}) != body_fingerprint({"a": 2})


# -- GoalDeliveryTranslator (github + jira) ----------------------------------
def test_goal_delivery_github_translates() -> None:
    ev = GoalDeliveryTranslator().translate(
        source="github", raw_payload=_GH_MERGED,
        headers={"x-github-delivery": "guid-1"}, now=100)
    assert isinstance(ev, InboundEvent)
    assert ev.source == "github" and ev.type == "pull_request.merged"
    assert ev.normalized["prNumber"] == 987 and ev.normalized["baseRef"] == "develop"
    assert ev.idempotency_key == "github:pr:ByteDeskAI/bytedesk-platform#987:guid-1"
    assert "x-hub-signature-256" not in ev.headers


def test_goal_delivery_github_key_stable_on_redelivery() -> None:
    t = GoalDeliveryTranslator()
    a = t.translate(source="github", raw_payload=_GH_MERGED, headers={"x-github-delivery": "g"}, now=1)
    b = t.translate(source="github", raw_payload=_GH_MERGED, headers={"x-github-delivery": "g"}, now=9)
    assert a.idempotency_key == b.idempotency_key  # redelivery dedupes despite different now


def test_goal_delivery_non_merge_returns_none() -> None:
    opened = {"action": "opened", "pull_request": {"number": 1}}
    assert GoalDeliveryTranslator().translate(source="github", raw_payload=opened, headers={}, now=1) is None


def test_goal_delivery_jira_key_includes_status_category() -> None:
    ev = GoalDeliveryTranslator().translate(
        source="jira", raw_payload=_JIRA_DONE,
        headers={"x-atlassian-webhook-identifier": "wh-1"}, now=100)
    assert ev.type == "jira.issue_updated" and ev.normalized["statusCategory"] == "done"
    assert ev.idempotency_key == "jira:BDP-1235:done:wh-1"


def test_goal_delivery_unknown_source_returns_none() -> None:
    assert GoalDeliveryTranslator().translate(source="gitlab", raw_payload={}, headers={}, now=1) is None


# -- AgenticInboxTranslator --------------------------------------------------
def test_agentic_inbox_translates() -> None:
    ev = AgenticInboxTranslator().translate(source="agentic-inbox", raw_payload=_EMAIL, headers={}, now=100)
    assert ev.type == "email.received" and ev.idempotency_key == "agentic-inbox:evt-1"
    assert ev.normalized["mailboxId"] == "maya@x.com"  # normalized lowercases
    assert ev.occurred_at == 100  # ISO received_at not coerced into epoch


def test_agentic_inbox_invalid_payload_returns_none() -> None:
    assert AgenticInboxTranslator().translate(source="agentic-inbox", raw_payload={"event_id": ""}, headers={}, now=1) is None


# -- SignalTranslator --------------------------------------------------------
def test_signal_translator_keys_by_source_matchkey_body() -> None:
    ev = SignalTranslator(match_key_for=lambda s, h: "build.finished").translate(
        source="teamcity", raw_payload={"version": "1.2.3"}, headers={}, now=5)
    assert ev.type == "signal.deliver" and ev.normalized["matchKey"] == "build.finished"
    assert ev.idempotency_key.startswith("signal:teamcity:build.finished:")


# -- registry ----------------------------------------------------------------
def test_registry_resolves_known_channels_and_none_for_unknown() -> None:
    assert isinstance(resolve_translator(CHANNEL_GOAL_DELIVERY), GoalDeliveryTranslator)
    assert isinstance(resolve_translator(CHANNEL_AGENTIC_INBOX), AgenticInboxTranslator)
    assert isinstance(resolve_translator(CHANNEL_SIGNAL), SignalTranslator)
    assert resolve_translator("nonsense") is None
