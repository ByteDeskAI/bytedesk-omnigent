"""Signed inbound-webhook / event ingress (BDP-2249, ADR-0142).

The omnigent-native answer to "agents react where the world happens": a signed
``POST /v1/ingress/{source}`` verifies an HMAC-SHA256 signature, resolves a
``(source, match_key)`` binding, and **delivers a durable signal** to the signal
bus (BDP-2248) — waking the parked session (e.g. TeamCity ``build.finished`` →
``release:{version}`` resumes the awaiting release run). An unmatched event 404s
(never 2xx — a permissive ingress is a silent-drop + double-fire hole, BDP-1419);
a bad signature 401s; a replayed event 409s (the bus's idempotent AlreadyResolved).

The ingress logic (``process_inbound``) is pure + injectable (store + bus) so it
is unit-proven without FastAPI; the route is thin glue.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from base64 import b64decode
from binascii import Error as BinasciiError
from base64 import b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from inspect import Parameter, signature
from typing import Protocol, runtime_checkable

from sqlalchemy import select

from bytedesk_omnigent.bus.signal_bus import DeliveryStatus
from bytedesk_omnigent.db_models import SqlWebhookBinding
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


class IngressStatus(str, Enum):
    """The outcome of an inbound event."""

    DELIVERED = "delivered"  # signal delivered to a parked session (202)
    ALREADY_RESOLVED = "already_resolved"  # replayed event (409)
    NO_BINDING = "no_binding"  # no (source, event) binding (404, never 2xx)
    BAD_SIGNATURE = "bad_signature"  # HMAC mismatch (401)
    DEAD_LETTERED = "dead_lettered"  # bound but no pending wait (404)
    EXPIRED = "expired"  # the wait expired before this late deliver (410, never 2xx)


@dataclass(frozen=True)
class IngressResult:
    """Result of ``process_inbound`` — carries the HTTP status the route returns."""

    status: IngressStatus
    http_status: int
    signal_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class WebhookBinding:
    """A row of ``webhook_bindings``."""

    id: str
    source: str
    match_key: str
    signal_id: str
    enabled: bool


def verify_hmac_signature(raw_body: bytes, secret: str, provided: str) -> bool:
    """Constant-time HMAC-SHA256 hex verification of a raw request body.

    Accepts a bare hex digest or the ``sha256=<hex>`` form (GitHub style).
    """
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided_hex = provided.split("=", 1)[1] if "=" in provided else provided
    return hmac.compare_digest(expected, provided_hex)


SecretResolver = Callable[[str], "str | None"]


def secret_env_name(source: str) -> str:
    """Return the conventional env var that carries *source*'s webhook secret."""
    return f"OMNIGENT_INGRESS_SECRET_{source.upper().replace('-', '_')}"


def default_secret_resolver(source: str) -> str | None:
    """Resolve a source's webhook secret from the environment.

    ``OMNIGENT_INGRESS_SECRET_<SOURCE>`` (uppercased, ``-`` → ``_``). Returns
    ``None`` when the source has no configured secret (the route 404s — an
    unconfigured source is not a valid ingress target).
    """
    env_key = secret_env_name(source)
    return os.environ.get(env_key)


# Injectable secret-resolver Strategy (BDP-2349 #16). Default = the env resolver
# above; a deployment that keeps webhook secrets in a vault registers a different
# resolver via `set_secret_resolver` instead of being hardwired to env. The route
# resolves through `resolve_secret`, never `default_secret_resolver` directly.
_secret_resolver: SecretResolver = default_secret_resolver


def set_secret_resolver(resolver: SecretResolver | None) -> None:
    """Install the active webhook secret *resolver* (``None`` restores the default)."""
    global _secret_resolver
    _secret_resolver = resolver if resolver is not None else default_secret_resolver


def resolve_secret(source: str) -> str | None:
    """Resolve *source*'s webhook secret via the active resolver Strategy."""
    return _secret_resolver(source)


# ── per-source webhook signature adapter (BDP-2354) ──────────────────────────
# An Adapter (ADR-0008) per webhook source owns BOTH halves of the source's
# wire contract: how it signs the body (verify) and where it carries the event
# name (match_key). The old code hardwired HMAC-SHA256 + a fixed header list, so
# a source that signs differently (or names its event in a different header) had
# nowhere to live. A registry keyed by source resolves the adapter; GitHub is the
# registered default for any source without a bespoke adapter. The adapter
# composes with the existing SecretResolver — the route resolves the secret via
# `resolve_secret` and passes it into `verify`, so the two seams stay orthogonal.


@runtime_checkable
class WebhookSourceAdapter(Protocol):
    """A webhook source's signature + event-routing contract (ADR-0008)."""

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Return whether *raw_body* is authentic for *secret* given *headers*."""
        ...

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        """Return the binding match key (event name), ``"*"`` if none.

        Header-only adapters can ignore ``raw_body`` and ``payload``; providers
        such as Stripe or JSON payload relays use body/payload data to route to
        deterministic bindings.
        """


@dataclass(frozen=True)
class WebhookAdapterDescriptor:
    """Setup-safe metadata for a webhook adapter's external contract."""

    source: str
    signature_headers: tuple[str, ...]
    event_headers: tuple[str, ...]
    auth_scheme: str
    match_key_fallback: str = "*"
    secret_env: str | None = None

    def __post_init__(self) -> None:
        if self.secret_env is None:
            object.__setattr__(self, "secret_env", secret_env_name(self.source))

    def as_dict(self) -> dict[str, object]:
        """Return JSON-serializable metadata safe for platform display."""
        return {
            "source": self.source,
            "signature_headers": list(self.signature_headers),
            "event_headers": list(self.event_headers),
            "auth_scheme": self.auth_scheme,
            "match_key_fallback": self.match_key_fallback,
            "secret_env": self.secret_env,
        }


_DEFAULT_WEBHOOK_DESCRIPTOR = WebhookAdapterDescriptor(
    source="github",
    signature_headers=("x-omnigent-signature", "x-hub-signature-256"),
    event_headers=("x-omnigent-event",),
    auth_scheme="hmac_sha256",
)


class GitHubWebhookAdapter:
    """Default adapter: HMAC-SHA256 over the raw body (GitHub / TeamCity style).

    Reads the signature from ``X-Omnigent-Signature`` (preferred) or GitHub's
    ``X-Hub-Signature-256`` (bare hex or ``sha256=<hex>``); the event name from
    GitHub's standard ``X-GitHub-Event`` header or the legacy
    ``X-Omnigent-Event`` shim (``"*"`` when absent). Headers are read
    case-insensitively.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-omnigent-signature") or _header(
            headers, "x-hub-signature-256"
        )
        if not provided:
            return False
        return verify_hmac_signature(raw_body, secret, provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-github-event") or _header(headers, "x-omnigent-event") or "*"


class SlackWebhookAdapter:
    """Slack Events API adapter: verify ``v0`` signatures and route by event type.

    Slack signs ``v0:{timestamp}:{raw_body}`` with HMAC-SHA256 in
    ``X-Slack-Signature`` and includes the UNIX timestamp in
    ``X-Slack-Request-Timestamp``. Events do not carry the routing event name in
    a header, so this adapter reads the parsed JSON payload supplied by
    ``process_inbound`` and emits stable match keys:

    - ``event_callback:<event.type>`` for normal Events API callbacks
    - top-level ``type`` (for example ``url_verification``) otherwise
    - ``"*"`` when no payload event type is available
    """

    _VERSION = "v0"
    _MAX_SKEW_SECONDS = 60 * 5

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-slack-signature")
        timestamp = _header(headers, "x-slack-request-timestamp")
        if not provided or not timestamp:
            return False
        try:
            request_ts = int(timestamp)
        except ValueError:
            return False
        if abs(int(time.time()) - request_ts) > self._MAX_SKEW_SECONDS:
            return False
        base = b":".join((self._VERSION.encode(), timestamp.encode(), raw_body))
        digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"{self._VERSION}={digest}", provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (headers, raw_body)
        if not isinstance(payload, Mapping):
            return "*"
        event = payload.get("event")
        if isinstance(event, Mapping):
            event_type = event.get("type")
            if isinstance(event_type, str) and event_type:
                top_level = payload.get("type")
                prefix = top_level if isinstance(top_level, str) and top_level else "event"
                return f"{prefix}:{event_type}"
        top_level = payload.get("type")
        return top_level if isinstance(top_level, str) and top_level else "*"


class StripeWebhookAdapter:
    """Stripe Events API adapter.

    Stripe signs ``{timestamp}.{raw_body}`` in the ``Stripe-Signature`` header
    (``t=...``, one or more ``v1=...`` signatures). The event type lives in the
    JSON payload's ``type`` field, so this adapter is intentionally body-aware.
    A five-minute replay window matches Stripe's recommended default.
    """

    def __init__(self, *, tolerance_seconds: int = 300) -> None:
        self._tolerance_seconds = tolerance_seconds

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        header = _header(headers, "stripe-signature")
        values = _parse_stripe_signature(header)
        timestamps = values.get("t", [])
        signatures = values.get("v1", [])
        if not timestamps or not signatures:
            return False
        try:
            timestamp = int(timestamps[0])
        except ValueError:
            return False
        if abs(int(time.time()) - timestamp) > self._tolerance_seconds:
            return False
        signed_payload = str(timestamp).encode("utf-8") + b"." + raw_body
        expected = hmac.new(
            secret.encode("utf-8"), signed_payload, hashlib.sha256
        ).hexdigest()
        return any(hmac.compare_digest(expected, candidate) for candidate in signatures)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (headers, raw_body)
        event_type = payload.get("type") if payload is not None else None
        return event_type if isinstance(event_type, str) and event_type else "*"


class JsonPayloadWebhookAdapter:
    """HMAC adapter for SaaS webhooks whose event name lives in JSON payloads.

    Many work-management systems (Jira, Trello, Asana, and Notion-like relays)
    do not expose a stable event header. They POST a signed JSON body with the
    routing signal in fields such as ``event``, ``type``, ``webhookEvent``,
    ``issue_event_type_name``, or nested ``action.type``. This adapter keeps the
    default Omnigent/GitHub HMAC verification contract while deterministically
    deriving the binding match key from those payload fields.
    """

    event_paths = (
        "event",
        "type",
        "webhookEvent",
        "webhook_event",
        "event_type",
        "issue_event_type_name",
        "action.type",
        "action",
    )

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return GitHubWebhookAdapter().verify(raw_body, headers, secret)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        header_event = _header(headers, "x-omnigent-event")
        if header_event:
            return header_event
        if payload is None:
            try:
                decoded = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except (UnicodeDecodeError, ValueError):
                return "*"
            payload = decoded if isinstance(decoded, Mapping) else None
        if not isinstance(payload, Mapping):
            return "*"
        for path in self.event_paths:
            value = _json_path(payload, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "*"


class MicrosoftTeamsWebhookAdapter:
    """Microsoft Teams outgoing-webhook adapter.

    Teams signs the raw request body with HMAC-SHA256 and sends the digest as a
    base64 value in ``Authorization: HMAC <digest>``. It does not provide a
    first-class event-type header, so native Teams messages route to the stable
    ``message`` binding by default while relays may still override the key via
    ``X-Omnigent-Event``.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "authorization")
        if not provided:
            return False
        scheme, _, value = provided.partition(" ")
        provided_digest = value if scheme.lower() == "hmac" and value else provided
        try:
            actual = b64decode(provided_digest, validate=True)
        except (BinasciiError, ValueError):
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(expected, actual)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-omnigent-event") or "message"


class LinearWebhookAdapter:
    """Linear issue/project webhook adapter.

    Linear signs the raw JSON body with HMAC-SHA256 in ``Linear-Signature`` and
    carries the routable event in the payload rather than a header. Omnigent maps
    ``{"type":"Issue","action":"update"}`` to the binding key
    ``Issue.update`` so one agent can await specific work-management events while
    another uses the ``*`` catch-all for broad triage.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "linear-signature")
        if not provided:
            return False
        return verify_hmac_signature(raw_body, secret, provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = raw_body
        if payload is not None:
            event_type = payload.get("type")
            action = payload.get("action")
            if isinstance(event_type, str) and event_type:
                if isinstance(action, str) and action:
                    return f"{event_type}.{action}"
                return event_type
            if isinstance(action, str) and action:
                return action
        return _header(headers, "linear-event") or "*"


class ShopifyWebhookAdapter:
    """Shopify webhook adapter: base64 HMAC body signature + topic routing.

    Shopify signs the raw request body with HMAC-SHA256 and sends the digest as
    base64 in ``X-Shopify-Hmac-Sha256``. The event topic lives in
    ``X-Shopify-Topic`` (for example ``orders/create`` or ``app/uninstalled``),
    which becomes the Omnigent binding ``match_key``.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-shopify-hmac-sha256")
        if not provided:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        try:
            actual = base64.b64decode(provided, validate=True)
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(expected, actual)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-shopify-topic") or "*"


class DiscordWebhookAdapter:
    """Discord interaction/webhook adapter using Ed25519 request signatures.

    Discord signs ``X-Signature-Timestamp + raw_body`` with the application's
    Ed25519 private key and sends the hex signature in
    ``X-Signature-Ed25519``. The configured ingress "secret" is the Discord
    application public key as a hex string. ``X-Discord-Event`` optionally
    carries a routable event name; absent means the per-source ``"*"`` binding.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        signature = _header(headers, "x-signature-ed25519")
        timestamp = _header(headers, "x-signature-timestamp")
        if not signature or not timestamp:
            return False
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError:
            return False

        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(secret))
            public_key.verify(
                bytes.fromhex(signature), timestamp.encode("utf-8") + raw_body
            )
        except (InvalidSignature, ValueError):
            return False
        return True

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-discord-event") or "*"


class TrelloWebhookAdapter:
    """Trello webhook adapter using its HMAC-SHA1 callback contract.

    Trello sends ``X-Trello-Webhook`` as a base64-encoded HMAC-SHA1 digest over
    ``raw_body + callback_url`` using the app secret. Omnigent expects the edge
    or route deployment to supply the callback URL it registered with Trello via
    ``X-Trello-Callback-Url`` so verification remains deterministic without
    hardcoding deployment URLs. ``X-Trello-Action-Type`` is the binding match key
    when present; otherwise the source-level ``"*"`` binding can catch it.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-trello-webhook")
        callback_url = _header(headers, "x-trello-callback-url")
        if not provided or not callback_url:
            return False
        try:
            provided_digest = b64decode(provided, validate=True)
        except ValueError:
            return False
        signed = raw_body + callback_url.encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha1).digest()
        return hmac.compare_digest(expected, provided_digest)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-trello-action-type") or "*"


class ZendeskWebhookAdapter:
    """Zendesk webhook adapter for support-ticket automation.

    Zendesk signs ``timestamp + raw_body`` with HMAC-SHA256 and base64-encodes
    the digest in ``X-Zendesk-Webhook-Signature``. Omnigent deployments attach
    ``X-Omnigent-Event`` to the Zendesk webhook configuration so ticket events
    can route to exact bindings while still supporting the ``"*"`` catch-all.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        signature = _header(headers, "x-zendesk-webhook-signature")
        timestamp = _header(headers, "x-zendesk-webhook-signature-timestamp")
        if not signature or not timestamp:
            return False
        expected = hmac.new(
            secret.encode("utf-8"), timestamp.encode("utf-8") + raw_body, hashlib.sha256
        ).digest()
        return hmac.compare_digest(b64encode(expected).decode("ascii"), signature)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-omnigent-event") or "*"


class HubSpotWebhookAdapter:
    """HubSpot legacy webhook adapter.

    HubSpot's legacy webhook signature is ``sha256(clientSecret + rawBody)`` in
    ``X-HubSpot-Signature``. Unlike GitHub-style sources, HubSpot carries the
    event route inside the JSON body (usually a list of event objects with
    ``subscriptionType``), so this adapter extracts the first event type from the
    raw payload and falls back to ``"*"`` for account-wide catch-all bindings.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-hubspot-signature")
        if not provided:
            return False
        expected = hashlib.sha256(secret.encode("utf-8") + raw_body).hexdigest()
        return hmac.compare_digest(expected, provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = headers
        if payload is None:
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except (UnicodeDecodeError, ValueError, TypeError):
                return "*"
        event = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(event, dict):
            return "*"
        for field in ("subscriptionType", "eventType", "event_type", "type"):
            value = event.get(field)
            if isinstance(value, str) and value.strip():
                return value
        return "*"


class AsanaWebhookAdapter:
    """Asana webhook adapter for ``POST /v1/ingress/asana``.

    Asana signs delivery bodies with ``X-Hook-Signature`` using HMAC-SHA256 and
    the webhook secret. Asana's native payload carries an ``events`` list rather
    than a single event header, so the adapter reads a ByteDesk/edge-proxy
    ``X-Asana-Event`` routing header when present and otherwise falls back to the
    per-source ``"*"`` catch-all binding.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-hook-signature")
        if not provided:
            return False
        return verify_hmac_signature(raw_body, secret, provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = (raw_body, payload)
        return _header(headers, "x-asana-event") or "*"


class JiraWebhookAdapter:
    """Jira Cloud webhook adapter for issue/project automation events.

    Jira's durable event name is carried in the JSON body as ``webhookEvent``
    (for example ``jira:issue_created``), not a standard event header. This
    built-in adapter lets Omnigent bind Jira events directly to parked agent
    sessions while retaining the default HMAC-SHA256 shared-secret verification
    expected by Omnigent ingress deployments and API gateways.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-omnigent-signature") or _header(
            headers, "x-hub-signature-256"
        )
        if not provided:
            return False
        return verify_hmac_signature(raw_body, secret, provided)

    def match_key(
        self,
        headers: Mapping[str, str],
        *,
        raw_body: bytes | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        _ = raw_body
        if payload is not None:
            event = payload.get("webhookEvent")
            if isinstance(event, str) and event:
                return event
        return _header(headers, "x-atlassian-event") or _header(headers, "x-omnigent-event") or "*"
class IntercomWebhookAdapter:
    """Intercom adapter: HMAC-SHA1 signature + topic-based event routing.

    Intercom webhooks sign the raw body in ``X-Hub-Signature`` using a SHA1 HMAC
    (bare hex or ``sha1=<hex>``). The routed event topic lives in ``X-Topic``.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-hub-signature")
        if not provided:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
        provided_hex = provided.split("=", 1)[1] if "=" in provided else provided
        return hmac.compare_digest(expected, provided_hex)

    def match_key(self, headers: Mapping[str, str]) -> str:
        return _header(headers, "x-topic") or "*"
class GitLabWebhookAdapter:
    """GitLab webhook adapter: shared-token verification + GitLab event header.

    GitLab sends the configured webhook secret in ``X-Gitlab-Token`` and the
    event kind in ``X-Gitlab-Event``. This adapter lets Omnigent bind GitLab
    merge request, pipeline, issue, and push hooks to durable signals without
    deployments registering a custom source adapter.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        del raw_body
        provided = _header(headers, "x-gitlab-token")
        if not provided:
            return False
        return hmac.compare_digest(provided, secret)

    def match_key(self, headers: Mapping[str, str]) -> str:
        return _header(headers, "x-gitlab-event") or "*"


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (Starlette ``Headers`` is already CI, but a
    plain dict in tests is not) — returns ``""`` when absent."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def _parse_stripe_signature(header: str) -> dict[str, list[str]]:
    """Parse Stripe's comma-delimited signature header into a multimap."""
    values: dict[str, list[str]] = {}
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep or not key:
            continue
        values.setdefault(key, []).append(value)
    return values


def _adapter_match_key(
    adapter: WebhookSourceAdapter,
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    payload: Mapping[str, object] | None,
) -> str:
    """Call body-aware adapters while preserving legacy adapter signatures."""
    parameters = signature(adapter.match_key).parameters
    supports_body = any(
        param.kind is Parameter.VAR_KEYWORD or name in {"raw_body", "payload"}
        for name, param in parameters.items()
    )
    if supports_body:
        return adapter.match_key(headers, raw_body=raw_body, payload=payload)
    try:
        return adapter.match_key(headers)  # type: ignore[call-arg]
    except TypeError:
        return adapter.match_key(raw_body, headers)  # type: ignore[call-arg]


def _json_path(payload: Mapping[str, object], dotted_path: str) -> object | None:
    """Read a dotted JSON path from a decoded object, returning ``None`` if absent."""
    current: object = payload
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current



def _build_webhook_adapter_registry():
    """The per-source webhook-adapter registry (BDP-2354).

    GitHub is the registered default — `resolve_webhook_adapter` returns it for
    any source without a bespoke adapter. A deployment registers a source-specific
    adapter (different signing scheme / event header) by name.
    """
    from omnigent.pluggable import PluggableRegistry

    registry: PluggableRegistry[WebhookSourceAdapter] = PluggableRegistry[
        WebhookSourceAdapter
    ](
        "webhook_source", default=("github", GitHubWebhookAdapter)
    )
    registry.register("slack", SlackWebhookAdapter)
    registry.register("stripe", StripeWebhookAdapter)
    for source in ("json", "jira", "trello", "asana", "notion"):
        registry.register(source, JsonPayloadWebhookAdapter)
    registry.register("microsoft-teams", MicrosoftTeamsWebhookAdapter)
    registry.register("teams", MicrosoftTeamsWebhookAdapter)
    registry.register("linear", LinearWebhookAdapter)
    registry.register("shopify", ShopifyWebhookAdapter)
    registry.register("discord", DiscordWebhookAdapter)
    registry.register("trello", TrelloWebhookAdapter)
    registry.register("zendesk", ZendeskWebhookAdapter)
    registry.register("asana", AsanaWebhookAdapter)
    registry: PluggableRegistry[WebhookSourceAdapter] = PluggableRegistry(
        "webhook_source", default=("github", lambda: GitHubWebhookAdapter())
    )
    registry.register("hubspot", lambda: HubSpotWebhookAdapter())
    default: tuple[str, Callable[[], WebhookSourceAdapter]] = (
        "github",
        GitHubWebhookAdapter,
    )
    registry: PluggableRegistry[WebhookSourceAdapter] = PluggableRegistry(
        "webhook_source", default=default
    )
    registry.register("jira", JiraWebhookAdapter)
    registry.register("intercom", IntercomWebhookAdapter)
    registry.register("gitlab", GitLabWebhookAdapter)
    return registry


def _adapter_match_key(
    adapter: WebhookSourceAdapter,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None,
) -> str:
    """Resolve an adapter match key with payload-aware adapters.

    Registered third-party adapters may still implement the original one-arg
    ``match_key(headers)`` shape, so keep them compatible while allowing built-in
    body-aware adapters (Jira, Notion, Intercom) to route from JSON payloads.
    """
    try:
        return adapter.match_key(headers, payload)
    except TypeError:
        return adapter.match_key(headers)  # type: ignore[call-arg]


# Lazily-built singleton so a deployment can register adapters once at import.
_webhook_adapter_registry = None
_webhook_adapter_descriptors: dict[str, WebhookAdapterDescriptor] = {
    "github": _DEFAULT_WEBHOOK_DESCRIPTOR
}


def register_webhook_adapter(
    source: str,
    factory: Callable[[], WebhookSourceAdapter],
    *,
    descriptor: WebhookAdapterDescriptor | None = None,
) -> None:
    """Register a per-source webhook adapter *factory* (BDP-2354)."""
    global _webhook_adapter_registry
    if _webhook_adapter_registry is None:
        _webhook_adapter_registry = _build_webhook_adapter_registry()
    _webhook_adapter_registry.register(source, factory)
    if descriptor is not None:
        if descriptor.source != source:
            raise ValueError(
                f"descriptor source {descriptor.source!r} does not match adapter {source!r}"
            )
        _webhook_adapter_descriptors[source] = descriptor


def resolve_webhook_adapter(source: str) -> WebhookSourceAdapter:
    """Resolve *source*'s webhook adapter, falling back to the GitHub default."""
    global _webhook_adapter_registry
    if _webhook_adapter_registry is None:
        _webhook_adapter_registry = _build_webhook_adapter_registry()
    if source in _webhook_adapter_registry.names():
        return _webhook_adapter_registry.get(source)
    return _webhook_adapter_registry.resolve_default()


def describe_webhook_adapters() -> list[dict[str, object]]:
    """Return setup-safe metadata for registered webhook adapters.

    This intentionally omits secret values and returns only the headers,
    match-key fallback, and conventional env var ByteDesk Platform needs to guide
    an operator through connecting a third-party webhook source.
    """
    global _webhook_adapter_registry
    if _webhook_adapter_registry is None:
        _webhook_adapter_registry = _build_webhook_adapter_registry()
    descriptors: list[WebhookAdapterDescriptor] = []
    for source in sorted(_webhook_adapter_registry.names()):
        descriptors.append(
            _webhook_adapter_descriptors.get(source)
            or WebhookAdapterDescriptor(
                source=source,
                signature_headers=(),
                event_headers=(),
                auth_scheme="custom",
            )
        )
    return [descriptor.as_dict() for descriptor in descriptors]


def _to_binding(row: SqlWebhookBinding) -> WebhookBinding:
    return WebhookBinding(
        id=row.id,
        source=row.source,
        match_key=row.match_key,
        signal_id=row.signal_id,
        enabled=row.enabled,
    )


class IngressBindingStore:
    """Durable store of ``(source, match_key) -> signal_id`` webhook bindings."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    def register_binding(
        self, *, source: str, match_key: str, signal_id: str, now: int | None = None
    ) -> WebhookBinding:
        """Register (idempotently by ``(source, match_key)``) a webhook binding."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            existing = session.execute(
                select(SqlWebhookBinding).where(
                    SqlWebhookBinding.source == source,
                    SqlWebhookBinding.match_key == match_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.signal_id = signal_id
                existing.enabled = True
                session.flush()
                return _to_binding(existing)
            row = SqlWebhookBinding(
                id=f"wh_{uuid.uuid4().hex}",
                source=source,
                match_key=match_key,
                signal_id=signal_id,
                enabled=True,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_binding(row)

    def resolve_binding(self, *, source: str, match_key: str) -> WebhookBinding | None:
        """Resolve the binding for ``(source, match_key)``.

        Prefers an exact ``match_key`` over the per-source ``"*"`` catch-all.
        Disabled bindings are ignored.
        """
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlWebhookBinding).where(
                        SqlWebhookBinding.source == source,
                        SqlWebhookBinding.enabled.is_(True),
                    )
                )
                .scalars()
                .all()
            )
            # Convert inside the session — the ORM rows detach on exit.
            exact = next((r for r in rows if r.match_key == match_key), None)
            star = next((r for r in rows if r.match_key == "*"), None)
            chosen = exact or star
            return _to_binding(chosen) if chosen is not None else None

    def list_bindings(
        self, *, source: str | None = None, enabled: bool | None = None
    ) -> list[WebhookBinding]:
        """List webhook bindings for operator/API management.

        Results are deterministic by source then match key so ByteDesk Platform can
        diff the registered ingress surface without reading the database directly.
        """
        with self._session() as session:
            stmt = select(SqlWebhookBinding)
            if source is not None:
                stmt = stmt.where(SqlWebhookBinding.source == source)
            if enabled is not None:
                stmt = stmt.where(SqlWebhookBinding.enabled.is_(enabled))
            rows = session.execute(
                stmt.order_by(SqlWebhookBinding.source, SqlWebhookBinding.match_key)
            ).scalars().all()
            return [_to_binding(row) for row in rows]


def process_inbound(
    *,
    source: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    store: IngressBindingStore,
    bus,
    adapter: WebhookSourceAdapter | None = None,
    payload: dict | None = None,
    now: int | None = None,
) -> IngressResult:
    """Verify → resolve → deliver. The pure ingress logic (injectable store + bus).

    The per-source :class:`WebhookSourceAdapter` (BDP-2354) owns BOTH signature
    verification (``verify(raw_body, headers, secret)``) and event routing
    (``match_key(headers, raw_body=..., payload=...)``); it defaults to the
    GitHub HMAC-SHA256 adapter. The *secret* is resolved by the caller via the
    existing SecretResolver and passed in, so the two seams compose.

    Returns an :class:`IngressResult` carrying the HTTP status the route returns:
    bad signature → 401; no binding → 404 (never 2xx, BDP-1419); delivered → 202;
    replayed → 409; bound-but-no-waiter → 404 (dead-lettered).
    """
    if adapter is None:
        adapter = GitHubWebhookAdapter()
    if not adapter.verify(raw_body, headers, secret):
        return IngressResult(IngressStatus.BAD_SIGNATURE, 401, detail="signature mismatch")
    match_key = _adapter_match_key(
        adapter, headers=headers, raw_body=raw_body, payload=payload
    )
    match_key = _adapter_match_key(adapter, raw_body, headers)
    match_key = _adapter_match_key(adapter, headers, payload)
    binding = store.resolve_binding(source=source, match_key=match_key)
    if binding is None:
        return IngressResult(
            IngressStatus.NO_BINDING, 404, detail=f"no binding for {source}/{match_key}"
        )
    result = bus.deliver(signal_id=binding.signal_id, payload=payload, now=now)
    if result.status is DeliveryStatus.DELIVERED:
        return IngressResult(IngressStatus.DELIVERED, 202, signal_id=binding.signal_id)
    if result.status is DeliveryStatus.ALREADY_RESOLVED:
        return IngressResult(
            IngressStatus.ALREADY_RESOLVED, 409, signal_id=binding.signal_id,
            detail="already resolved",
        )
    if result.status is DeliveryStatus.EXPIRED:
        # The wait expired before this late deliver — the parked session was never
        # woken (dead-lettered for recovery). 410 (never 2xx, BDP-1419) so the
        # sender retries/alerts rather than treating it as benignly handled.
        return IngressResult(
            IngressStatus.EXPIRED, 410, signal_id=binding.signal_id,
            detail="wait expired before delivery",
        )
    return IngressResult(
        IngressStatus.DEAD_LETTERED, 404, signal_id=binding.signal_id,
        detail="no pending wait for signal",
    )


def preview_inbound(
    *,
    source: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    store: IngressBindingStore,
    adapter: WebhookSourceAdapter | None = None,
) -> IngressResult:
    """Verify and resolve an inbound event without delivering it.

    This deterministic preflight harness lets ByteDesk Platform and connected-app
    installers prove a webhook's signature, event extraction, and binding target
    before enabling autonomous delivery. It deliberately stops before
    ``bus.deliver`` so a setup wizard can safely test production credentials
    against a parked signal without waking the agent.

    Returns bad signature → 401, no binding → 404, matched preflight → 200.
    """
    if adapter is None:
        adapter = GitHubWebhookAdapter()
    if not adapter.verify(raw_body, headers, secret):
        return IngressResult(IngressStatus.BAD_SIGNATURE, 401, detail="signature mismatch")
    match_key = adapter.match_key(headers)
    binding = store.resolve_binding(source=source, match_key=match_key)
    if binding is None:
        return IngressResult(
            IngressStatus.NO_BINDING, 404, detail=f"no binding for {source}/{match_key}"
        )
    return IngressResult(
        IngressStatus.DELIVERED, 200, signal_id=binding.signal_id,
        detail="preflight matched; delivery not attempted",
    )
def _adapter_match_key(
    adapter: WebhookSourceAdapter,
    raw_body: bytes,
    headers: Mapping[str, str],
) -> str:
    """Call source adapters with body-aware routing while preserving old plugins."""
    try:
        return adapter.match_key(raw_body, headers)
    except TypeError:
        return adapter.match_key(headers)  # type: ignore[call-arg]


# Lazily-built, per-URI cache of the binding store (mirrors the other accessors).
_binding_store_cache: dict[str, IngressBindingStore] = {}


def get_binding_store() -> IngressBindingStore:
    """Return the durable webhook-binding store (BDP-2249, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _binding_store_cache.get(location)
    if store is None:
        store = IngressBindingStore(location)
        _binding_store_cache[location] = store
    return store
