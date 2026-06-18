"""Tests for the outreach compliance floor: suppression store + CAN-SPAM policy
(BDP-2278 F3, ADR-0142)."""
from __future__ import annotations

from omnigent.compliance import SqlAlchemySuppressionStore
from omnigent.policies.builtins.outreach_compliance import outreach_compliance


def _store(tmp_path) -> SqlAlchemySuppressionStore:
    return SqlAlchemySuppressionStore(f"sqlite:///{tmp_path / 'sup.db'}")


# ── suppression store ────────────────────────────────────────────────────────


def test_suppress_is_idempotent_and_normalizes_address(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.suppress(channel="email", address="A@X.com", reason="unsubscribe") is True
    # Same address, different casing/whitespace → already suppressed.
    assert store.suppress(channel="email", address=" a@x.com ", reason="unsubscribe") is False
    assert store.is_suppressed(channel="email", address="a@x.COM") is True


def test_is_suppressed_is_channel_scoped(tmp_path) -> None:
    store = _store(tmp_path)
    store.suppress(channel="email", address="a@x.com", reason="gdpr_erasure")
    assert store.is_suppressed(channel="email", address="a@x.com") is True
    assert store.is_suppressed(channel="sms", address="a@x.com") is False
    assert store.is_suppressed(channel="email", address="b@x.com") is False


# ── compliance policy ────────────────────────────────────────────────────────


def _send(name: str, args: dict) -> dict:
    return {"type": "tool_call", "data": {"name": name, "arguments": args}}


def test_denies_outreach_without_unsubscribe() -> None:
    evaluate = outreach_compliance(["email\\.send"])
    result = evaluate(_send("email.send", {"to": "a@x.com", "body": "buy now"}))
    assert result["result"] == "DENY"
    assert "unsubscribe" in result["reason"].lower()


def test_allows_outreach_with_unsubscribe() -> None:
    evaluate = outreach_compliance(["email\\.send"])
    args = {"to": "a@x.com", "body": "hi", "unsubscribe_url": "https://x/u"}
    assert evaluate(_send("email.send", args))["result"] == "ALLOW"


def test_denies_suppressed_recipient_via_injected_checker(tmp_path) -> None:
    store = _store(tmp_path)
    store.suppress(channel="email", address="a@x.com", reason="unsubscribe")
    evaluate = outreach_compliance(["email\\.send"], is_suppressed=store.is_suppressed)
    args = {"to": "A@x.com", "unsubscribe": "https://x/u"}
    result = evaluate(_send("email.send", args))
    assert result["result"] == "DENY"
    assert "do-not-contact" in result["reason"]


def test_allows_non_outreach_and_non_tool_events() -> None:
    evaluate = outreach_compliance(["email\\.send"])
    assert evaluate(_send("read.file", {}))["result"] == "ALLOW"
    assert evaluate({"type": "llm_call"})["result"] == "ALLOW"
