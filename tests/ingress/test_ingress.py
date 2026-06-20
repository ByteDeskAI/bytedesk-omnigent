"""Tests for the signed inbound-webhook ingress: HMAC verify + resolve + deliver
end-to-end through the durable signal bus (BDP-2249, ADR-0142)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

import bytedesk_omnigent.ingress as _ingress_mod
from bytedesk_omnigent.bus import SqlAlchemySignalBus
from bytedesk_omnigent.ingress import (
    DeclarativeHmacWebhookAdapter,
    GitHubWebhookAdapter,
    IngressBindingStore,
    IngressStatus,
    WebhookSourceAdapter,
    process_inbound,
    register_declarative_hmac_webhook_adapter,
    register_webhook_adapter,
    resolve_webhook_adapter,
    verify_hmac_signature,
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _hdrs(signature: str, event: str) -> dict[str, str]:
    """GitHub-style ingress headers (signature + event name) for the default
    adapter (BDP-2354)."""
    return {"x-omnigent-signature": signature, "x-omnigent-event": event}


def test_verify_hmac_signature_bare_and_prefixed() -> None:
    body = b'{"x":1}'
    secret = "s3cr3t"
    assert verify_hmac_signature(body, secret, _sign(body, secret)) is True
    assert verify_hmac_signature(body, secret, "sha256=" + _sign(body, secret)) is True
    assert verify_hmac_signature(body, secret, "deadbeef") is False


def test_process_inbound_delivers_to_signal_bus_then_replay_409(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'ing.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "teamcity-secret"

    # A parked release run awaits the signal; a binding maps the webhook to it.
    bus.register_wait(
        signal_id="release:1.2.3", session_id="sess-rel", key="subscribe:teamcity",
        kind="subscribe", target="teamcity", now=now,
    )
    store.register_binding(
        source="teamcity", match_key="build.finished", signal_id="release:1.2.3", now=now
    )

    body = json.dumps({"build": "green"}).encode()

    # Bad signature -> 401, never reaches the bus.
    bad = process_inbound(
        source="teamcity", raw_body=body, headers=_hdrs("nope", "build.finished"),
        secret=secret, store=store, bus=bus, payload={"build": "green"},
    )
    assert bad.status is IngressStatus.BAD_SIGNATURE
    assert bad.http_status == 401

    # Valid signature but no binding for the event -> 404 (never 2xx, BDP-1419).
    nob = process_inbound(
        source="teamcity", raw_body=body,
        headers=_hdrs(_sign(body, secret), "unknown.event"),
        secret=secret, store=store, bus=bus, payload=None,
    )
    assert nob.status is IngressStatus.NO_BINDING
    assert nob.http_status == 404

    # Valid + bound -> delivers to the bus, resolves the parked wait -> 202.
    ok = process_inbound(
        source="teamcity", raw_body=body,
        headers=_hdrs(_sign(body, secret), "build.finished"),
        secret=secret, store=store, bus=bus,
        payload={"build": "green"}, now=now + 1,
    )
    assert ok.status is IngressStatus.DELIVERED
    assert ok.http_status == 202
    assert ok.signal_id == "release:1.2.3"
    assert bus.list_pending(target="teamcity") == []  # the run was woken

    # Replayed delivery (TeamCity retry) -> idempotent 409, no double-fire.
    again = process_inbound(
        source="teamcity", raw_body=body,
        headers=_hdrs(_sign(body, secret), "build.finished"),
        secret=secret, store=store, bus=bus,
        payload=None, now=now + 2,
    )
    assert again.status is IngressStatus.ALREADY_RESOLVED
    assert again.http_status == 409


def test_star_binding_is_per_source_catch_all(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'ing2.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "sec"
    bus.register_wait(
        signal_id="sig:any", session_id="s", key="k", kind="subscribe",
        target="gh", now=now,
    )
    store.register_binding(source="github", match_key="*", signal_id="sig:any", now=now)
    body = b"{}"
    res = process_inbound(
        source="github", raw_body=body,
        headers=_hdrs(_sign(body, secret), "some.event.we.didnt.bind"),
        secret=secret, store=store, bus=bus,
        payload=None, now=now + 1,
    )
    assert res.status is IngressStatus.DELIVERED  # fell back to the "*" catch-all


def test_process_inbound_expired_wait_returns_410_not_409(tmp_path) -> None:
    """A webhook arriving after its wait expired maps to 410 EXPIRED (never a 2xx,
    and distinct from the benign-replay 409) so the sender retries/alerts instead
    of treating an un-woken parked session as already handled (BDP-2283 #1)."""
    db = f"sqlite:///{tmp_path / 'ing3.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "teamcity-secret"
    bus.register_wait(
        signal_id="release:9.9.9", session_id="sess-rel", key="subscribe:teamcity",
        kind="subscribe", target="teamcity", expires_at=now + 60, now=now,
    )
    store.register_binding(
        source="teamcity", match_key="build.finished", signal_id="release:9.9.9", now=now
    )
    assert bus.sweep_expired(now=now + 61) == 1

    body = json.dumps({"build": "green"}).encode()
    res = process_inbound(
        source="teamcity", raw_body=body,
        headers=_hdrs(_sign(body, secret), "build.finished"),
        secret=secret, store=store, bus=bus,
        payload={"build": "green"}, now=now + 62,
    )
    assert res.status is IngressStatus.EXPIRED
    assert res.http_status == 410


# ── per-source webhook signature adapter (BDP-2354) ──────────────────────────


@pytest.fixture()
def _restore_adapter_registry():
    """Restore the module-level adapter registry after a test registers one."""
    saved = _ingress_mod._webhook_adapter_registry
    yield
    _ingress_mod._webhook_adapter_registry = saved


def test_github_default_adapter_verifies_hmac_and_reads_event() -> None:
    """The default GitHub adapter satisfies the Protocol, HMAC-verifies the raw
    body (bare + ``sha256=`` forms), and reads the event header (BDP-2354)."""
    adapter = GitHubWebhookAdapter()
    assert isinstance(adapter, WebhookSourceAdapter)
    body = b'{"build":"green"}'
    secret = "s3cr3t"
    sig = _sign(body, secret)

    assert adapter.verify(body, {"x-omnigent-signature": sig}, secret) is True
    assert adapter.verify(body, {"x-hub-signature-256": "sha256=" + sig}, secret) is True
    assert adapter.verify(body, {"x-omnigent-signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False  # no signature header
    assert adapter.match_key({"x-omnigent-event": "build.finished"}) == "build.finished"
    assert adapter.match_key({}) == "*"  # absent → catch-all


def test_resolve_webhook_adapter_defaults_to_github() -> None:
    """A source with no bespoke adapter falls back to the GitHub default (BDP-2354)."""
    assert isinstance(resolve_webhook_adapter("anything"), GitHubWebhookAdapter)


def test_second_registered_source_uses_its_own_adapter(_restore_adapter_registry) -> None:
    """A second source registers its own signature scheme + event header; the
    registry resolves it instead of the GitHub default (BDP-2354)."""

    class _StripeAdapter:
        # A different scheme: a shared-token header (not HMAC) and a different
        # event header — proving the adapter owns BOTH halves of the contract.
        def verify(self, raw_body, headers, secret) -> bool:
            return headers.get("stripe-token") == secret

        def match_key(self, headers) -> str:
            return headers.get("stripe-event", "*")

    register_webhook_adapter("stripe", _StripeAdapter)
    adapter = resolve_webhook_adapter("stripe")
    assert isinstance(adapter, _StripeAdapter)
    assert adapter.verify(b"{}", {"stripe-token": "whsec"}, "whsec") is True
    assert adapter.verify(b"{}", {"stripe-token": "wrong"}, "whsec") is False
    assert adapter.match_key({"stripe-event": "invoice.paid"}) == "invoice.paid"
    # The default source is untouched.
    assert isinstance(resolve_webhook_adapter("github"), GitHubWebhookAdapter)


def test_declarative_hmac_adapter_supports_header_only_saas_contracts() -> None:
    """A no-code integration can describe its HMAC signature + event headers
    instead of shipping a bespoke adapter class for every SaaS webhook source."""
    body = b'{"record":"rec_123"}'
    secret = "airtable-or-notion-secret"
    signature = _sign(body, secret)
    adapter = DeclarativeHmacWebhookAdapter(
        signature_header="x-saas-signature",
        event_header="x-saas-event",
        signature_prefix="v1=",
        default_event="webhook.notification",
    )

    assert adapter.verify(
        body,
        {"X-SaaS-Signature": f"v1={signature}", "X-SaaS-Event": "record.changed"},
        secret,
    ) is True
    assert adapter.verify(body, {"x-saas-signature": signature}, secret) is False
    assert adapter.verify(body, {"x-saas-signature": "v1=deadbeef"}, secret) is False
    assert adapter.match_key({"X-SaaS-Event": "record.changed"}) == "record.changed"
    assert adapter.match_key({}) == "webhook.notification"


def test_register_declarative_hmac_adapter_resolves_for_source(
    _restore_adapter_registry,
) -> None:
    """Integration manifests can install a SaaS source adapter by source name."""
    body = b'{"issue":"ISS-1"}'
    secret = "shared"
    signature = _sign(body, secret)

    register_declarative_hmac_webhook_adapter(
        "jira-lite",
        signature_header="x-jira-lite-signature",
        event_header="x-jira-lite-event",
        signature_prefix="sha256=",
    )

    adapter = resolve_webhook_adapter("jira-lite")
    assert adapter.verify(
        body,
        {"x-jira-lite-signature": f"sha256={signature}"},
        secret,
    ) is True
    assert adapter.match_key({"x-jira-lite-event": "issue.updated"}) == "issue.updated"
