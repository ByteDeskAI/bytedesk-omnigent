"""Tests for the signed inbound-webhook ingress: HMAC verify + resolve + deliver
end-to-end through the durable signal bus (BDP-2249, ADR-0142)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from base64 import b64encode
from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bytedesk_omnigent.ingress as _ingress_mod
from bytedesk_omnigent.bus import SqlAlchemySignalBus
from bytedesk_omnigent.ingress import (
    AirtableWebhookAdapter,
    AsanaWebhookAdapter,
    CloudEventsWebhookAdapter,
    DeclarativeHmacWebhookAdapter,
    DiscordWebhookAdapter,
    GitHubWebhookAdapter,
    GitLabWebhookAdapter,
    GoogleWorkspaceWebhookAdapter,
    HubSpotWebhookAdapter,
    IngressBindingStore,
    IngressStatus,
    MondayWebhookAdapter,
    ServiceNowWebhookAdapter,
    IntercomWebhookAdapter,
    JiraWebhookAdapter,
    JsonPayloadWebhookAdapter,
    LinearWebhookAdapter,
    MicrosoftTeamsWebhookAdapter,
    ShopifyWebhookAdapter,
    SlackWebhookAdapter,
    StripeWebhookAdapter,
    TrelloWebhookAdapter,
    WebhookAdapterDescriptor,
    WebhookSourceAdapter,
    ZendeskWebhookAdapter,
    describe_webhook_adapters,
    preview_inbound,
    process_inbound,
    register_declarative_hmac_webhook_adapter,
    register_webhook_adapter,
    resolve_webhook_adapter,
    verify_hmac_signature,
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _stripe_signature(body: bytes, secret: str, timestamp: int) -> str:
    signed = str(timestamp).encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _shopify_sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")
def _sign_sha1(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def _hdrs(signature: str, event: str) -> dict[str, str]:
    """GitHub-style ingress headers (signature + event name) for the default
    adapter (BDP-2354)."""
    return {"x-omnigent-signature": signature, "x-omnigent-event": event}


def _slack_sign(body: bytes, secret: str, timestamp: int) -> str:
    base = b":".join((b"v0", str(timestamp).encode(), body))
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


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


def test_preview_inbound_verifies_and_resolves_without_delivering(tmp_path) -> None:
    """The ingress preflight harness validates the signed event and binding without
    waking the parked agent, so Platform can test connected-app routing safely."""
    db = f"sqlite:///{tmp_path / 'preview.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "preview-secret"
    bus.register_wait(
        signal_id="ticket:zendesk:123", session_id="sess-support", key="k",
        kind="subscribe", target="zendesk", now=now,
    )
    store.register_binding(
        source="zendesk", match_key="ticket.created", signal_id="ticket:zendesk:123",
        now=now,
    )
    body = json.dumps({"ticket": 123}).encode()

    preview = preview_inbound(
        source="zendesk", raw_body=body,
        headers=_hdrs(_sign(body, secret), "ticket.created"),
        secret=secret, store=store,
    )

    assert preview.status is IngressStatus.DELIVERED
    assert preview.http_status == 200
    assert preview.signal_id == "ticket:zendesk:123"
    assert preview.detail == "preflight matched; delivery not attempted"
    assert bus.list_pending(target="zendesk")[0].signal_id == "ticket:zendesk:123"


def test_preview_inbound_reports_bad_signature_and_missing_binding(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'preview-errors.db'}"
    store = IngressBindingStore(db)
    secret = "preview-secret"
    body = b"{}"

    bad = preview_inbound(
        source="hubspot", raw_body=body, headers=_hdrs("bad", "contact.created"),
        secret=secret, store=store,
    )
    assert bad.status is IngressStatus.BAD_SIGNATURE
    assert bad.http_status == 401

    missing = preview_inbound(
        source="hubspot", raw_body=body,
        headers=_hdrs(_sign(body, secret), "contact.created"), secret=secret,
        store=store,
    )
    assert missing.status is IngressStatus.NO_BINDING
    assert missing.http_status == 404


# ── per-source webhook signature adapter (BDP-2354) ──────────────────────────


@pytest.fixture()
def _restore_adapter_registry():
    """Restore the module-level adapter registry after a test registers one."""
    saved = _ingress_mod._webhook_adapter_registry
    saved_descriptors = dict(_ingress_mod._webhook_adapter_descriptors)
    _ingress_mod._webhook_adapter_registry = None
    yield
    _ingress_mod._webhook_adapter_registry = saved
    _ingress_mod._webhook_adapter_descriptors = saved_descriptors


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


def test_github_default_adapter_reads_real_github_event_header() -> None:
    """GitHub webhooks carry routing in ``X-GitHub-Event``; the first-party
    adapter must resolve that header directly so repository events can wake
    Omnigent bindings without a ByteDesk-specific shim."""
    adapter = GitHubWebhookAdapter()
    body = b'{"action":"opened","issue":{"number":123}}'
    secret = "github-webhook-secret"
    sig = _sign(body, secret)

    assert adapter.verify(body, {"X-Hub-Signature-256": "sha256=" + sig}, secret) is True
    assert adapter.match_key({"X-GitHub-Event": "issues"}) == "issues"


def test_process_inbound_delivers_real_github_event_header(tmp_path) -> None:
    """A GitHub issue event signed with GitHub's standard headers resolves the
    ``github/issues`` binding and wakes the parked agent session."""
    db = f"sqlite:///{tmp_path / 'github-ingress.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "github-webhook-secret"
    bus.register_wait(
        signal_id="github:issue:123",
        session_id="sess-gh",
        key="subscribe:github:issues",
        kind="subscribe",
        target="github",
        now=now,
    )
    store.register_binding(
        source="github", match_key="issues", signal_id="github:issue:123", now=now
    )
    body = b'{"action":"opened","issue":{"number":123}}'

    result = process_inbound(
        source="github",
        raw_body=body,
        headers={
            "X-Hub-Signature-256": "sha256=" + _sign(body, secret),
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-123",
        },
        secret=secret,
        store=store,
        bus=bus,
        payload={"action": "opened", "issue": {"number": 123}},
        now=now + 1,
    )

    assert result.status is IngressStatus.DELIVERED
    assert result.http_status == 202
    assert result.signal_id == "github:issue:123"
def test_microsoft_teams_adapter_verifies_authorization_hmac_and_routes_message() -> None:
    """Teams outgoing webhooks sign the raw body with a base64 HMAC in the
    Authorization header; Omnigent should support that native wire contract
    without a custom relay translating it to GitHub-style headers."""
    adapter = MicrosoftTeamsWebhookAdapter()
    body = json.dumps({"text": "@omni summarize this incident"}).encode()
    secret = "teams-shared-secret"
    signature = b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

    assert adapter.verify(body, {"authorization": f"HMAC {signature}"}, secret) is True
    assert adapter.verify(body, {"Authorization": f"hmac {signature}"}, secret) is True
    assert adapter.verify(body, {"authorization": signature}, secret) is True
    assert adapter.verify(body, {"authorization": "HMAC bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({}) == "message"
    assert adapter.match_key({"x-omnigent-event": "teams.incident"}) == "teams.incident"


def test_resolve_webhook_adapter_defaults_to_github() -> None:
    """A source with no bespoke adapter falls back to the GitHub default (BDP-2354)."""
    assert isinstance(resolve_webhook_adapter("anything"), GitHubWebhookAdapter)


def test_slack_adapter_verifies_signature_and_routes_payload_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack Events API payloads route to durable signals by body-derived event type."""
    db = f"sqlite:///{tmp_path / 'slack.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "slack-signing-secret"
    payload = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {"type": "app_mention", "text": "@omni triage this"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "x-slack-request-timestamp": str(now),
        "x-slack-signature": _slack_sign(body, secret, now),
    }
    monkeypatch.setattr(_ingress_mod.time, "time", lambda: now)

    assert isinstance(resolve_webhook_adapter("slack"), SlackWebhookAdapter)
    adapter = SlackWebhookAdapter()
    assert adapter.verify(body, headers, secret) is True
    assert adapter.match_key(headers, payload=payload) == "event_callback:app_mention"

    bus.register_wait(
        signal_id="slack:app_mention",
        session_id="sess-slack",
        key="subscribe:slack",
        kind="subscribe",
        target="slack",
        now=now,
    )
    store.register_binding(
        source="slack",
        match_key="event_callback:app_mention",
        signal_id="slack:app_mention",
        now=now,
    )

    result = process_inbound(
        source="slack",
        raw_body=body,
        headers=headers,
        secret=secret,
        store=store,
        bus=bus,
        adapter=adapter,
        payload=payload,
        now=now + 1,
    )

    assert result.status is IngressStatus.DELIVERED
    assert result.http_status == 202
    assert result.signal_id == "slack:app_mention"


def test_stripe_adapter_verifies_signature_and_routes_by_payload_type(monkeypatch) -> None:
    """Stripe is a built-in body-aware adapter: signature over timestamp + body,
    match key from the event payload's ``type`` field."""
    now = 1_700_000_000
    monkeypatch.setattr(_ingress_mod.time, "time", lambda: now)
    adapter = StripeWebhookAdapter()
    body = json.dumps({"id": "evt_123", "type": "invoice.paid"}).encode()
    secret = "whsec_stripe"
    headers = {"Stripe-Signature": _stripe_signature(body, secret, now)}

    assert isinstance(resolve_webhook_adapter("stripe"), StripeWebhookAdapter)
    assert adapter.verify(body, headers, secret) is True
    assert adapter.verify(body, {"Stripe-Signature": "t=bad,v1=nope"}, secret) is False
    assert adapter.verify(
        body,
        {"Stripe-Signature": _stripe_signature(body, secret, now - 301)},
        secret,
    ) is False
    assert adapter.match_key(headers, payload={"type": "invoice.paid"}) == "invoice.paid"
    assert adapter.match_key(headers, payload={}) == "*"


def test_process_inbound_uses_body_aware_stripe_match_key(monkeypatch, tmp_path) -> None:
    """A Stripe event can wake exactly the binding for its signed payload type."""
    now = 1_700_000_000
    monkeypatch.setattr(_ingress_mod.time, "time", lambda: now)
    db = f"sqlite:///{tmp_path / 'stripe.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    secret = "whsec_stripe"
    bus.register_wait(
        signal_id="billing:invoice-paid", session_id="sess-billing", key="subscribe:stripe",
        kind="subscribe", target="stripe", now=now,
    )
    store.register_binding(
        source="stripe", match_key="invoice.paid", signal_id="billing:invoice-paid", now=now
    )
    body = json.dumps({"id": "evt_123", "type": "invoice.paid"}).encode()
    payload = {"id": "evt_123", "type": "invoice.paid"}
    result = process_inbound(
        source="stripe",
        raw_body=body,
        headers={"Stripe-Signature": _stripe_signature(body, secret, now)},
        secret=secret,
        store=store,
        bus=bus,
        adapter=resolve_webhook_adapter("stripe"),
        payload=payload,
        now=now,
    )

    assert result.status is IngressStatus.DELIVERED
    assert result.http_status == 202
    assert result.signal_id == "billing:invoice-paid"


def test_slack_adapter_rejects_stale_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack replay protection rejects requests outside the five-minute window."""
    body = b'{"type":"url_verification"}'
    secret = "slack-signing-secret"
    request_ts = 1_700_000_000
    monkeypatch.setattr(_ingress_mod.time, "time", lambda: request_ts + 301)
    headers = {
        "x-slack-request-timestamp": str(request_ts),
        "x-slack-signature": _slack_sign(body, secret, request_ts),
    }

    assert SlackWebhookAdapter().verify(body, headers, secret) is False
def test_resolve_webhook_adapter_has_builtin_microsoft_teams_adapter() -> None:
    """Microsoft Teams is a first-party connected-app ingress source so Platform
    installs can target /v1/ingress/microsoft-teams directly."""
    assert isinstance(resolve_webhook_adapter("microsoft-teams"), MicrosoftTeamsWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("teams"), MicrosoftTeamsWebhookAdapter)


def test_process_inbound_delivers_microsoft_teams_message_to_signal_bus(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'teams.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "teams-shared-secret"
    body = json.dumps({"text": "@omni triage INC-42"}).encode()
    signature = b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

    bus.register_wait(
        signal_id="teams:incident:42", session_id="sess-teams", key="subscribe:teams",
        kind="subscribe", target="microsoft-teams", now=now,
    )
    store.register_binding(
        source="microsoft-teams", match_key="message", signal_id="teams:incident:42", now=now
    )

    res = process_inbound(
        source="microsoft-teams",
        raw_body=body,
        headers={"Authorization": f"HMAC {signature}"},
        secret=secret,
        store=store,
        bus=bus,
        adapter=resolve_webhook_adapter("microsoft-teams"),
        payload={"text": "@omni triage INC-42"},
        now=now + 1,
    )

    assert res.status is IngressStatus.DELIVERED
    assert res.http_status == 202
    assert res.signal_id == "teams:incident:42"
def test_asana_adapter_verifies_x_hook_signature_and_reads_event() -> None:
    """Asana webhooks sign the raw body in ``X-Hook-Signature``. The built-in
    adapter makes Asana a first-class ingress source instead of requiring each
    deployment to hand-register the same header mapping."""
    adapter = resolve_webhook_adapter("asana")
    assert isinstance(adapter, AsanaWebhookAdapter)

    body = b'{"events":[{"action":"changed"}]}'
    secret = "asana-webhook-secret"
    signature = _sign(body, secret)

    assert adapter.verify(body, {"x-hook-signature": signature}, secret) is True
    assert adapter.verify(body, {"X-Hook-Signature": signature}, secret) is True
    assert adapter.verify(body, {"x-hook-signature": "sha256=" + signature}, secret) is True
    assert adapter.verify(body, {"x-hook-signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({"x-asana-event": "task.changed"}) == "task.changed"
    assert adapter.match_key({}) == "*"


def test_intercom_adapter_verifies_sha1_signature_and_reads_topic() -> None:
    """Intercom sends HMAC-SHA1 in ``X-Hub-Signature`` and event topics in
    ``X-Topic``; the built-in adapter maps that contract into ingress bindings.
    """
    adapter = IntercomWebhookAdapter()
    body = b'{"type":"notification_event","topic":"conversation.user.created"}'
    secret = "intercom-client-secret"
    sig = _sign_sha1(body, secret)

    assert isinstance(adapter, WebhookSourceAdapter)
    assert adapter.verify(body, {"x-hub-signature": "sha1=" + sig}, secret) is True
    assert adapter.verify(body, {"x-hub-signature": sig}, secret) is True
    assert adapter.verify(body, {"x-hub-signature": "sha1=bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert (
        adapter.match_key({"x-topic": "conversation.user.created"})
        == "conversation.user.created"
    )
    assert adapter.match_key({}) == "*"


def test_resolve_webhook_adapter_has_built_in_intercom_adapter() -> None:
    """Intercom is a first-class source, not a deployment-local custom adapter."""
    assert isinstance(resolve_webhook_adapter("intercom"), IntercomWebhookAdapter)


def test_gitlab_adapter_verifies_shared_token_and_reads_event() -> None:
    """GitLab webhooks use their shared secret token header rather than HMAC.

    Registering a built-in adapter lets agents bind GitLab merge request and
    pipeline events without each deployment hand-writing the token/event mapping.
    """
    adapter = resolve_webhook_adapter("gitlab")

    assert isinstance(adapter, GitLabWebhookAdapter)
    assert isinstance(adapter, WebhookSourceAdapter)
    assert adapter.verify(b"{}", {"x-gitlab-token": "s3cr3t"}, "s3cr3t") is True
    assert adapter.verify(b"{}", {"x-gitlab-token": "wrong"}, "s3cr3t") is False
    assert adapter.verify(b"{}", {}, "s3cr3t") is False
    assert adapter.match_key({"x-gitlab-event": "Merge Request Hook"}) == "Merge Request Hook"
    assert adapter.match_key({}) == "*"
def test_google_workspace_adapter_verifies_channel_token_and_reads_resource_state() -> None:
    """Google Workspace push channels authenticate with X-Goog-Channel-Token.

    Route Drive/Calendar/Gmail changes by resource state so agents can bind to
    states such as ``exists`` while ``sync`` notifications remain separate.
    """
    adapter = GoogleWorkspaceWebhookAdapter()
    assert isinstance(adapter, WebhookSourceAdapter)

    headers = {
        "X-Goog-Channel-Token": "shared-channel-secret",
        "X-Goog-Resource-State": "exists",
        "X-Goog-Resource-ID": "drive-resource-123",
    }

    assert adapter.verify(b"", headers, "shared-channel-secret") is True
    assert adapter.verify(b"", headers, "wrong-secret") is False
    assert adapter.verify(b"", {}, "shared-channel-secret") is False
    assert adapter.match_key(headers) == "exists"
    assert adapter.match_key({"X-Goog-Channel-Token": "shared-channel-secret"}) == "*"


def test_google_workspace_source_resolves_bespoke_adapter() -> None:
    """The google-workspace source uses the push-channel token adapter."""
    assert isinstance(resolve_webhook_adapter("google-workspace"), GoogleWorkspaceWebhookAdapter)


def test_monday_adapter_verifies_signature_and_reads_monday_event() -> None:
    """Monday.com webhook ingress uses its own source adapter so teams can bind
    board/item automation events without custom deployment code."""
    adapter = MondayWebhookAdapter()
    body = b'{"event":{"type":"item.updated"}}'
    secret = "monday-signing-secret"
    sig = _sign(body, secret)

    assert adapter.verify(body, {"x-monday-signature": sig}, secret) is True
    assert adapter.verify(body, {"X-Monday-Signature": "sha256=" + sig}, secret) is True
    assert adapter.verify(body, {"x-monday-signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({"x-monday-event": "item.updated"}) == "item.updated"
    assert adapter.match_key({}) == "*"


def test_resolve_webhook_adapter_registers_monday_builtin() -> None:
    """The monday source resolves to the built-in adapter without app-specific
    bootstrap code, while unknown sources still use the safe GitHub-compatible default."""
    assert isinstance(resolve_webhook_adapter("monday"), MondayWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("monday.com"), MondayWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("unknown-service"), GitHubWebhookAdapter)

def test_servicenow_adapter_verifies_signature_and_reads_event() -> None:
    """ServiceNow incident/change events can wake agents through the shared
    ingress route with a ServiceNow-specific signature/event header contract."""
    adapter = ServiceNowWebhookAdapter()
    body = b'{"number":"INC0012345","sys_id":"abc123"}'
    secret = "servicenow-secret"
    sig = _sign(body, secret)

    assert adapter.verify(body, {"x-servicenow-signature": sig}, secret) is True
    assert adapter.verify(body, {"X-ServiceNow-Signature": "sha256=" + sig}, secret) is True
    assert adapter.verify(body, {"x-omnigent-signature": sig}, secret) is True
    assert adapter.verify(body, {"x-servicenow-signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({"x-servicenow-event": "incident.updated"}) == "incident.updated"
    assert adapter.match_key({"X-ServiceNow-Event": "change.approved"}) == "change.approved"
    assert adapter.match_key({}) == "*"

def test_resolve_webhook_adapter_registers_servicenow_builtin() -> None:
    """The built-in registry exposes ServiceNow without deployment-side glue."""
    assert isinstance(resolve_webhook_adapter("servicenow"), ServiceNowWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("service-now"), ServiceNowWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("github"), GitHubWebhookAdapter)


def test_second_registered_source_uses_its_own_adapter(_restore_adapter_registry) -> None:
    """A second source registers its own signature scheme + event header; the
    registry resolves it instead of the GitHub default (BDP-2354)."""

    class _BillingAdapter:
        # A different scheme: a shared-token header (not HMAC) and a different
        # event header — proving the adapter owns BOTH halves of the contract.
        def verify(self, raw_body, headers, secret) -> bool:
            return headers.get("billing-token") == secret

        def match_key(
            self,
            headers: Mapping[str, str],
            *,
            raw_body: bytes | None = None,
            payload: Mapping[str, object] | None = None,
        ) -> str:
            return headers.get("billing-event", "*")

    register_webhook_adapter("billing", _BillingAdapter)
    adapter = resolve_webhook_adapter("billing")
    assert isinstance(adapter, _BillingAdapter)
    assert adapter.verify(b"{}", {"billing-token": "whsec"}, "whsec") is True
    assert adapter.verify(b"{}", {"billing-token": "wrong"}, "whsec") is False
    assert adapter.match_key({"billing-event": "invoice.paid"}) == "invoice.paid"
    # The default source is untouched.
    assert isinstance(resolve_webhook_adapter("github"), GitHubWebhookAdapter)


def test_json_payload_adapter_routes_by_payload_event_fields() -> None:
    """JSON SaaS adapters can bind Jira/Trello/Asana-style payload event names
    without per-provider route code, while retaining the default HMAC contract."""
    adapter = JsonPayloadWebhookAdapter()
    body = json.dumps({"webhookEvent": "jira:issue_updated"}).encode()
    secret = "jira-secret"
    sig = _sign(body, secret)

    assert adapter.verify(body, {"x-omnigent-signature": sig}, secret) is True
    assert adapter.match_key({}, raw_body=body) == "jira:issue_updated"

    nested = json.dumps({"action": {"type": "card.updated"}}).encode()
    assert adapter.match_key({}, raw_body=nested) == "card.updated"
    assert adapter.match_key({"x-omnigent-event": "override"}, raw_body=body) == "override"
    assert adapter.match_key({}, raw_body=b"not-json") == "*"


def test_builtin_json_saas_sources_resolve_payload_adapter() -> None:
    assert isinstance(resolve_webhook_adapter("json"), JsonPayloadWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("notion"), JsonPayloadWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("jira"), JiraWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("trello"), TrelloWebhookAdapter)
    assert isinstance(resolve_webhook_adapter("asana"), AsanaWebhookAdapter)


def test_process_inbound_with_json_payload_adapter_delivers_bound_jira_event(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'jira.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "jira-secret"
    bus.register_wait(
        signal_id="issue:OPS-42", session_id="sess-jira", key="subscribe:jira",
        kind="subscribe", target="jira", now=now,
    )
    store.register_binding(
        source="jira", match_key="jira:issue_updated", signal_id="issue:OPS-42", now=now
    )
    payload = {"webhookEvent": "jira:issue_updated", "issue": "OPS-42"}
    body = json.dumps(payload).encode()

    res = process_inbound(
        source="jira", raw_body=body,
        headers={"x-omnigent-signature": _sign(body, secret)},
        secret=secret, store=store, bus=bus,
        adapter=resolve_webhook_adapter("jira"),
        payload=payload, now=now + 1,
    )

    assert res.status is IngressStatus.DELIVERED
    assert res.http_status == 202
    assert res.signal_id == "issue:OPS-42"


def test_jira_adapter_routes_on_webhook_event_payload(tmp_path) -> None:
    """Jira webhooks carry the durable event name in the JSON ``webhookEvent``
    field, so the built-in adapter must route on payload content instead of a
    source-specific header."""
    db = f"sqlite:///{tmp_path / 'jira-native.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "jira-secret"

    bus.register_wait(
        signal_id="jira:issue-created", session_id="sess-jira", key="subscribe:jira",
        kind="subscribe", target="jira", now=now,
    )
    store.register_binding(
        source="jira", match_key="jira:issue_created", signal_id="jira:issue-created", now=now
    )

    payload = {"webhookEvent": "jira:issue_created", "issue": {"key": "BDP-2371"}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    res = process_inbound(
        source="jira", raw_body=body,
        headers={"x-hub-signature-256": "sha256=" + _sign(body, secret)},
        secret=secret, store=store, bus=bus, adapter=JiraWebhookAdapter(),
        payload=payload, now=now + 1,
    )

    assert res.status is IngressStatus.DELIVERED
    assert res.http_status == 202
    assert res.signal_id == "jira:issue-created"


def test_jira_adapter_is_registered_builtin_source(_restore_adapter_registry) -> None:
    adapter = resolve_webhook_adapter("jira")
    assert isinstance(adapter, JiraWebhookAdapter)


def test_linear_adapter_verifies_signature_and_routes_by_payload() -> None:
    """Linear signs the raw body in ``Linear-Signature`` and carries the event
    discriminator in the JSON payload, so the built-in adapter composes a stable
    ``type.action`` match key for agent subscriptions.
    """
    adapter = LinearWebhookAdapter()
    body = json.dumps({"type": "Issue", "action": "update"}).encode()
    secret = "linear-secret"
    signature = _sign(body, secret)

    assert adapter.verify(body, {"Linear-Signature": signature}, secret) is True
    assert adapter.verify(body, {"Linear-Signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({}, payload={"type": "Issue", "action": "update"}) == "Issue.update"
    assert adapter.match_key({}, payload={"type": "Project"}) == "Project"
    assert adapter.match_key({}, payload={}) == "*"


def test_process_inbound_delivers_linear_payload_routed_event(tmp_path) -> None:
    """The ingress registry resolves ``source=linear`` to the built-in Linear
    adapter, allowing an agent to wait on a precise work-management event without
    a custom deployment hook.
    """
    db = f"sqlite:///{tmp_path / 'linear.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "linear-secret"
    payload = {"type": "Issue", "action": "update", "data": {"identifier": "ENG-42"}}
    body = json.dumps(payload).encode()

    bus.register_wait(
        signal_id="linear:issue:update", session_id="sess-linear", key="subscribe:linear",
        kind="subscribe", target="linear", now=now,
    )
    store.register_binding(
        source="linear", match_key="Issue.update", signal_id="linear:issue:update", now=now
    )

    result = process_inbound(
        source="linear", raw_body=body,
        headers={"Linear-Signature": _sign(body, secret)},
        secret=secret, store=store, bus=bus, payload=payload, now=now + 1,
        adapter=resolve_webhook_adapter("linear"),
    )

    assert result.status is IngressStatus.DELIVERED
    assert result.http_status == 202
    assert result.signal_id == "linear:issue:update"


def test_shopify_adapter_verifies_base64_hmac_and_routes_topic() -> None:
    """Shopify uses base64 HMAC signatures and X-Shopify-Topic routing."""
    adapter = ShopifyWebhookAdapter()
    assert isinstance(adapter, WebhookSourceAdapter)
    body = b'{"id":123,"total_price":"42.00"}'
    secret = "shopify-shared-secret"
    signature = _shopify_sign(body, secret)

    assert adapter.verify(
        body,
        {"X-Shopify-Hmac-Sha256": signature, "X-Shopify-Topic": "orders/create"},
        secret,
    ) is True
    assert adapter.verify(
        body,
        {"X-Shopify-Hmac-Sha256": "not-base64!"},
        secret,
    ) is False
    assert adapter.verify(
        body,
        {"X-Shopify-Hmac-Sha256": _shopify_sign(body, "wrong")},
        secret,
    ) is False
    assert adapter.match_key({"X-Shopify-Topic": "orders/create"}) == "orders/create"
    assert adapter.match_key({}) == "*"


def test_resolve_webhook_adapter_has_builtin_shopify_adapter() -> None:
    assert isinstance(resolve_webhook_adapter("shopify"), ShopifyWebhookAdapter)


def test_process_inbound_delivers_shopify_topic_to_signal_bus(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'shopify.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "shopify-secret"
    body = json.dumps({"id": 123, "total_price": "42.00"}).encode()

    bus.register_wait(
        signal_id="shopify:order:123",
        session_id="sess-commerce",
        key="subscribe:shopify",
        kind="subscribe",
        target="shopify",
        now=now,
    )
    store.register_binding(
        source="shopify",
        match_key="orders/create",
        signal_id="shopify:order:123",
        now=now,
    )

    result = process_inbound(
        source="shopify",
        raw_body=body,
        headers={
            "X-Shopify-Hmac-Sha256": _shopify_sign(body, secret),
            "X-Shopify-Topic": "orders/create",
        },
        secret=secret,
        store=store,
        bus=bus,
        adapter=resolve_webhook_adapter("shopify"),
        payload={"id": 123, "total_price": "42.00"},
        now=now + 1,
    )

    assert result.status is IngressStatus.DELIVERED
    assert result.http_status == 202
    assert result.signal_id == "shopify:order:123"
    assert bus.list_pending(target="shopify") == []


def test_discord_adapter_verifies_ed25519_signature_and_reads_event() -> None:
    """Discord signs interaction callbacks with Ed25519 over
    ``timestamp + raw_body``. The built-in adapter should let a hosted Omnigent
    bind Discord events without custom deployment code.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    timestamp = "1717171717"
    body = b'{"type":2,"data":{"name":"assign_agent"}}'
    signature = private_key.sign(timestamp.encode("utf-8") + body).hex()

    adapter = DiscordWebhookAdapter()
    assert isinstance(adapter, WebhookSourceAdapter)
    assert adapter.verify(
        body,
        {
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
            "X-Discord-Event": "interaction.create",
        },
        public_hex,
    ) is True
    assert adapter.verify(
        body,
        {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": "bad"},
        public_hex,
    ) is False
    assert adapter.match_key({"X-Discord-Event": "interaction.create"}) == "interaction.create"
    assert adapter.match_key({}) == "*"


def test_discord_adapter_is_registered_by_default() -> None:
    """The stock registry should resolve the Discord adapter without requiring
    deployment-specific registration.
    """
    assert isinstance(resolve_webhook_adapter("discord"), DiscordWebhookAdapter)
def test_trello_adapter_verifies_hmac_sha1_and_reads_action_type() -> None:
    """Trello signs raw_body + callback_url with HMAC-SHA1 and carries the
    action type separately for routing through webhook bindings."""
    adapter = TrelloWebhookAdapter()
    body = b'{"action":{"type":"cardMoved"}}'
    app_secret = "trello-app-secret"
    callback_url = "https://omnigent.example.com/v1/ingress/trello"
    signed = body + callback_url.encode("utf-8")
    signature = b64encode(
        hmac.new(app_secret.encode("utf-8"), signed, hashlib.sha1).digest()
    ).decode("ascii")

    headers = {
        "x-trello-webhook": signature,
        "x-trello-callback-url": callback_url,
        "x-trello-action-type": "cardMoved",
    }

    assert adapter.verify(body, headers, app_secret) is True
    assert adapter.verify(body, {**headers, "x-trello-webhook": "bad"}, app_secret) is False
    assert adapter.verify(body, {"x-trello-webhook": signature}, app_secret) is False
    assert adapter.match_key(headers) == "cardMoved"
    assert adapter.match_key({}) == "*"


def test_trello_adapter_is_registered_by_default() -> None:
    """Trello can be enabled by configuring OMNIGENT_INGRESS_SECRET_TRELLO;
    no deployment-specific adapter registration is required."""
    assert isinstance(resolve_webhook_adapter("trello"), TrelloWebhookAdapter)
def test_zendesk_adapter_verifies_timestamped_signature_and_event_header() -> None:
    """Zendesk signs ``timestamp + body`` with HMAC-SHA256/base64 and carries the
    routable event in an Omnigent-managed header so teams can bind ticket events
    to parked agents without custom glue."""
    body = b'{"ticket_id":123,"status":"open"}'
    secret = "zendesk-signing-secret"
    timestamp = "1712345678"
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()

    adapter = ZendeskWebhookAdapter()

    assert adapter.verify(
        body,
        {
            "x-zendesk-webhook-signature": signature,
            "x-zendesk-webhook-signature-timestamp": timestamp,
        },
        secret,
    ) is True
    assert adapter.verify(
        body,
        {
            "x-zendesk-webhook-signature": signature,
            "x-zendesk-webhook-signature-timestamp": "1712345679",
        },
        secret,
    ) is False
    assert adapter.verify(body, {"x-zendesk-webhook-signature": signature}, secret) is False
    assert adapter.match_key({"x-omnigent-event": "ticket.updated"}) == "ticket.updated"
    assert adapter.match_key({}) == "*"


def test_zendesk_adapter_is_registered_builtin() -> None:
    """Zendesk is a built-in ingress adapter, not caller-registered boilerplate."""
    assert isinstance(resolve_webhook_adapter("zendesk"), ZendeskWebhookAdapter)
def test_hubspot_adapter_verifies_legacy_signature_and_extracts_subscription_type() -> None:
    """HubSpot signs ``clientSecret + rawBody`` and carries the routing event in
    the JSON body, so the built-in adapter must not require a deployment shim."""
    adapter = HubSpotWebhookAdapter()
    body = json.dumps(
        [{"subscriptionType": "contact.propertyChange", "objectId": 123}],
        separators=(",", ":"),
    ).encode()
    secret = "hubspot-client-secret"
    signature = hashlib.sha256(secret.encode() + body).hexdigest()

    assert adapter.verify(body, {"X-HubSpot-Signature": signature}, secret) is True
    assert adapter.verify(body, {"X-HubSpot-Signature": "bad"}, secret) is False
    assert adapter.match_key({}, raw_body=body) == "contact.propertyChange"


def test_hubspot_source_is_registered_by_default() -> None:
    assert isinstance(resolve_webhook_adapter("hubspot"), HubSpotWebhookAdapter)


def test_webhook_adapter_manifest_describes_setup_contract(
    _restore_adapter_registry,
) -> None:
    """The ingress adapter manifest exposes setup-safe integration metadata.

    ByteDesk Platform can render the headers, event routing key, and secret env var
    an operator needs without seeing any actual secret values.
    """

    class _BearerAdapter:
        def verify(self, raw_body, headers, secret) -> bool:
            return headers.get("x-example-token") == secret

        def match_key(self, headers) -> str:
            return headers.get("x-example-event", "*")

    register_webhook_adapter(
        "examplecrm",
        _BearerAdapter,
        descriptor=WebhookAdapterDescriptor(
            source="examplecrm",
            signature_headers=("x-example-token",),
            event_headers=("x-example-event",),
            auth_scheme="shared_token",
            match_key_fallback="*",
            secret_env="OMNIGENT_INGRESS_SECRET_EXAMPLECRM",
        ),
    )

    manifest = describe_webhook_adapters()

    by_source = {entry["source"]: entry for entry in manifest}
    assert by_source["examplecrm"] == {
        "source": "examplecrm",
        "signature_headers": ["x-example-token"],
        "event_headers": ["x-example-event"],
        "auth_scheme": "shared_token",
        "match_key_fallback": "*",
        "secret_env": "OMNIGENT_INGRESS_SECRET_EXAMPLECRM",
    }
    assert by_source["github"] == {
        "source": "github",
        "signature_headers": ["x-omnigent-signature", "x-hub-signature-256"],
        "event_headers": ["x-omnigent-event"],
        "auth_scheme": "hmac_sha256",
        "match_key_fallback": "*",
        "secret_env": "OMNIGENT_INGRESS_SECRET_GITHUB",
    }


def test_ingress_router_exposes_webhook_adapter_manifest() -> None:
    """The HTTP surface exposes adapter setup metadata to ByteDesk Platform."""
    from bytedesk_omnigent.routes.ingress import create_ingress_router

    app = FastAPI()
    app.include_router(create_ingress_router(), prefix="/v1")

    response = TestClient(app).get("/v1/ingress/adapters")

    assert response.status_code == 200
    adapters = {entry["source"]: entry for entry in response.json()["adapters"]}
    assert adapters["github"] == {
        "source": "github",
        "signature_headers": ["x-omnigent-signature", "x-hub-signature-256"],
        "event_headers": ["x-omnigent-event"],
        "auth_scheme": "hmac_sha256",
        "match_key_fallback": "*",
        "secret_env": "OMNIGENT_INGRESS_SECRET_GITHUB",
    }
    assert "google-workspace" in adapters
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


def test_airtable_adapter_verifies_hmac_and_derives_event_from_payload() -> None:
    """Airtable routes by JSON payload shape when no explicit event header exists."""
    adapter = AirtableWebhookAdapter()
    body = json.dumps(
        {
            "base": {"id": "appBase"},
            "webhook": {"id": "achWebhook"},
            "actionMetadata": {"source": "client"},
        },
        separators=(",", ":"),
    ).encode()
    secret = "airtable-secret"
    signature = _sign(body, secret)

    assert adapter.verify(body, {"x-airtable-webhook-signature": signature}, secret) is True
    assert (
        adapter.verify(
            body, {"x-airtable-webhook-signature": "sha256=" + signature}, secret
        )
        is True
    )
    assert adapter.verify(body, {"x-airtable-webhook-signature": "bad"}, secret) is False
    assert adapter.verify(body, {}, secret) is False
    assert adapter.match_key({}, payload=json.loads(body)) == "base.changed"
    assert (
        adapter.match_key({}, payload={"webhook": {"id": "achWebhook"}})
        == "webhook.changed"
    )
    assert adapter.match_key({"x-omnigent-event": "override"}, payload={}) == "override"
    assert adapter.match_key({}, payload={}) == "*"


def test_airtable_source_is_registered_by_default(_restore_adapter_registry) -> None:
    assert isinstance(resolve_webhook_adapter("airtable"), AirtableWebhookAdapter)


def test_cloudevents_adapter_verifies_bearer_token_and_reads_ce_type() -> None:
    """CloudEvents-native providers authenticate with bearer/shared tokens."""
    adapter = CloudEventsWebhookAdapter()

    assert isinstance(adapter, WebhookSourceAdapter)
    assert adapter.verify(
        b'{"id":"evt_1"}', {"authorization": "Bearer shared-secret"}, "shared-secret"
    ) is True
    assert adapter.verify(
        b'{"id":"evt_1"}', {"authorization": "bearer shared-secret"}, "shared-secret"
    ) is True
    assert adapter.verify(
        b'{"id":"evt_1"}', {"x-omnigent-token": "shared-secret"}, "shared-secret"
    ) is True
    assert adapter.verify(
        b'{"id":"evt_1"}', {"authorization": "Bearer wrong"}, "shared-secret"
    ) is False
    assert adapter.verify(b'{"id":"evt_1"}', {}, "shared-secret") is False

    assert adapter.match_key({"ce-type": "com.salesforce.account.updated"}) == (
        "com.salesforce.account.updated"
    )
    assert adapter.match_key({"ce-source": "/accounts/123"}) == "*"


def test_salesforce_resolves_to_cloudevents_adapter(_restore_adapter_registry) -> None:
    assert isinstance(resolve_webhook_adapter("salesforce"), CloudEventsWebhookAdapter)
