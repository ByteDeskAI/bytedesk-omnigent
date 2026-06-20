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

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
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


def default_secret_resolver(source: str) -> str | None:
    """Resolve a source's webhook secret from the environment.

    ``OMNIGENT_INGRESS_SECRET_<SOURCE>`` (uppercased, ``-`` → ``_``). Returns
    ``None`` when the source has no configured secret (the route 404s — an
    unconfigured source is not a valid ingress target).
    """
    env_key = f"OMNIGENT_INGRESS_SECRET_{source.upper().replace('-', '_')}"
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

    def match_key(self, raw_body: bytes, headers: Mapping[str, str]) -> str:
        """The binding match key (event name) derived from *raw_body* / *headers*.

        Adapters that route exclusively by header can ignore *raw_body*. Payload-
        first adapters use it to support SaaS webhooks that carry the event name
        in JSON rather than a provider-specific header.
        """
        ...


class GitHubWebhookAdapter:
    """Default adapter: HMAC-SHA256 over the raw body (GitHub / TeamCity style).

    Reads the signature from ``X-Omnigent-Signature`` (preferred) or GitHub's
    ``X-Hub-Signature-256`` (bare hex or ``sha256=<hex>``); the event name from
    ``X-Omnigent-Event`` (``"*"`` when absent). Headers are read
    case-insensitively.
    """

    def verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        provided = _header(headers, "x-omnigent-signature") or _header(
            headers, "x-hub-signature-256"
        )
        if not provided:
            return False
        return verify_hmac_signature(raw_body, secret, provided)

    def match_key(self, raw_body: bytes, headers: Mapping[str, str]) -> str:
        _ = raw_body
        return _header(headers, "x-omnigent-event") or "*"


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

    def match_key(self, raw_body: bytes, headers: Mapping[str, str]) -> str:
        header_event = _header(headers, "x-omnigent-event")
        if header_event:
            return header_event
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, ValueError):
            return "*"
        if not isinstance(payload, dict):
            return "*"
        for path in self.event_paths:
            value = _json_path(payload, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "*"


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
    for source in ("json", "jira", "trello", "asana", "notion"):
        registry.register(source, JsonPayloadWebhookAdapter)
    return registry


# Lazily-built singleton so a deployment can register adapters once at import.
_webhook_adapter_registry = None


def register_webhook_adapter(
    source: str, factory: Callable[[], WebhookSourceAdapter]
) -> None:
    """Register a per-source webhook adapter *factory* (BDP-2354)."""
    global _webhook_adapter_registry
    if _webhook_adapter_registry is None:
        _webhook_adapter_registry = _build_webhook_adapter_registry()
    _webhook_adapter_registry.register(source, factory)


def resolve_webhook_adapter(source: str) -> WebhookSourceAdapter:
    """Resolve *source*'s webhook adapter, falling back to the GitHub default."""
    global _webhook_adapter_registry
    if _webhook_adapter_registry is None:
        _webhook_adapter_registry = _build_webhook_adapter_registry()
    if source in _webhook_adapter_registry.names():
        return _webhook_adapter_registry.get(source)
    return _webhook_adapter_registry.resolve_default()


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
    (``match_key(headers)``); it defaults to the GitHub HMAC-SHA256 adapter. The
    *secret* is resolved by the caller via the existing SecretResolver and passed
    in, so the two seams compose.

    Returns an :class:`IngressResult` carrying the HTTP status the route returns:
    bad signature → 401; no binding → 404 (never 2xx, BDP-1419); delivered → 202;
    replayed → 409; bound-but-no-waiter → 404 (dead-lettered).
    """
    if adapter is None:
        adapter = GitHubWebhookAdapter()
    if not adapter.verify(raw_body, headers, secret):
        return IngressResult(IngressStatus.BAD_SIGNATURE, 401, detail="signature mismatch")
    match_key = adapter.match_key(raw_body, headers)
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
