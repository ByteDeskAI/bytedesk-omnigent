"""Tests for the outreach compliance floor: suppression store + CAN-SPAM policy
(BDP-2278 F3, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.compliance import SqlAlchemySuppressionStore
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


def test_allows_outreach_with_unsubscribe(tmp_path) -> None:
    # Suppression now always runs (BDP-2285); inject an EMPTY store so the
    # not-suppressed precondition is explicit → ALLOW.
    evaluate = outreach_compliance(
        ["email\\.send"], is_suppressed=_store(tmp_path).is_suppressed
    )
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


def test_self_resolves_suppression_store_when_no_checker_injected(
    tmp_path, monkeypatch
) -> None:
    """On the production spec/factory_params path no callable is passed; the policy
    must self-resolve the durable suppression store, NOT silently skip suppression
    (BDP-2285 #1 — it was dead code, so a suppressed recipient was ALLOWed)."""
    store = _store(tmp_path)
    store.suppress(channel="email", address="a@x.com", reason="gdpr_erasure")
    monkeypatch.setattr("bytedesk_omnigent.compliance.get_suppression_store", lambda: store)
    evaluate = outreach_compliance(["email\\.send"])  # NO is_suppressed injected
    args = {"to": "a@x.com", "unsubscribe": "https://x/u"}
    assert evaluate(_send("email.send", args))["result"] == "DENY"


def test_denies_suppressed_recipient_in_list_cc_or_comma_joined(tmp_path) -> None:
    """A suppressed address evaded the gate when passed inside a list / cc / a
    comma-joined string (str([...]) never matched the store) — now any of them
    DENY (BDP-2285 #2)."""
    store = _store(tmp_path)
    store.suppress(channel="email", address="a@x.com", reason="unsubscribe")
    evaluate = outreach_compliance(["email\\.send"], is_suppressed=store.is_suppressed)

    # suppressed inside a list recipient
    assert (
        evaluate(_send("email.send", {"to": ["ok@x.com", "a@x.com"], "unsubscribe": "u"}))[
            "result"
        ]
        == "DENY"
    )
    # suppressed only in cc
    assert (
        evaluate(_send("email.send", {"to": "ok@x.com", "cc": "a@x.com", "unsubscribe": "u"}))[
            "result"
        ]
        == "DENY"
    )
    # suppressed in a comma-joined string
    assert (
        evaluate(_send("email.send", {"to": "ok@x.com, a@x.com", "unsubscribe": "u"}))[
            "result"
        ]
        == "DENY"
    )


def test_fails_closed_on_unparseable_recipient_shape(tmp_path) -> None:
    """A recipient shape the policy cannot parse into addresses fails CLOSED
    (DENY), never str()-coerced into one opaque token (BDP-2285 #2)."""
    store = _store(tmp_path)
    evaluate = outreach_compliance(["email\\.send"], is_suppressed=store.is_suppressed)
    args = {"to": {"addr": "a@x.com"}, "unsubscribe": "u"}  # dict, not str/list
    assert evaluate(_send("email.send", args))["result"] == "DENY"


def test_allows_non_outreach_and_non_tool_events() -> None:
    evaluate = outreach_compliance(["email\\.send"])
    assert evaluate(_send("read.file", {}))["result"] == "ALLOW"
    assert evaluate({"type": "llm_call"})["result"] == "ALLOW"
