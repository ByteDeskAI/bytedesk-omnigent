"""Deterministic idempotency keys for connected-app integration events.

Webhook providers retry aggressively. Before an autonomous agent turns an
external event into work, Omnigent needs a stable, secret-free ``(scope, key)``
that can be claimed in the durable idempotency store. This helper keeps that
contract pure and provider-neutral so ingress routes, workflow harnesses, and
future ByteDesk Platform adapters can share it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_PROVIDER_DELIVERY_HEADERS = (
    "x-github-delivery",
    "x-slack-request-timestamp",
    "stripe-signature",
    "x-linear-delivery",
    "x-atlassian-webhook-identifier",
    "x-hubspot-request-timestamp",
    "x-shopify-webhook-id",
    "x-zendesk-webhook-id",
    "x-airtable-webhook-id",
    "x-notion-webhook-id",
    "x-ms-notification-id",
    "x-discord-signature-timestamp",
)

_PAYLOAD_ID_PATHS = (
    ("id",),
    ("event", "id"),
    ("data", "id"),
    ("payload", "id"),
    ("issue", "id"),
    ("pull_request", "id"),
    ("object", "id"),
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class IntegrationIdempotencyKey:
    """A durable claim target for one connected-app event delivery."""

    scope: str
    key: str
    source: str
    event: str
    strategy: str


def build_integration_idempotency_key(
    *,
    source: str,
    event: str,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> IntegrationIdempotencyKey:
    """Build a deterministic, secret-free idempotency key for an integration event.

    Preference order:

    1. Provider delivery/correlation headers (best representation of a retry).
    2. Common payload object identifiers such as ``id`` or ``event.id``.
    3. SHA-256 of canonical JSON payload + source + event as a safe fallback.

    Sensitive headers are never copied blindly; only a small allowlist of known
    provider delivery headers is considered.
    """

    normalized_source = _slug(source)
    normalized_event = event.strip() or "*"
    scope = f"integration:{normalized_source}:{normalized_event}"
    normalized_headers = _lower_headers(headers or {})

    header_value = _first_header_value(normalized_headers)
    if header_value:
        return IntegrationIdempotencyKey(
            scope=scope,
            key=f"delivery:{header_value}",
            source=normalized_source,
            event=normalized_event,
            strategy="provider_delivery_id",
        )

    payload_id = _first_payload_id(payload or {})
    if payload_id is not None:
        path, value = payload_id
        return IntegrationIdempotencyKey(
            scope=scope,
            key=f"payload:{'.'.join(path)}:{value}",
            source=normalized_source,
            event=normalized_event,
            strategy="payload_identifier",
        )

    digest = hashlib.sha256(
        _canonical_json(
            {
                "source": normalized_source,
                "event": normalized_event,
                "payload": payload or {},
            }
        ).encode("utf-8")
    ).hexdigest()
    return IntegrationIdempotencyKey(
        scope=scope,
        key=f"payload_sha256:{digest}",
        source=normalized_source,
        event=normalized_event,
        strategy="canonical_payload_hash",
    )


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "unknown"


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value).strip() for name, value in headers.items()}


def _first_header_value(headers: Mapping[str, str]) -> str | None:
    for name in _PROVIDER_DELIVERY_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def _first_payload_id(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], str] | None:
    for path in _PAYLOAD_ID_PATHS:
        value = _get_path(payload, path)
        if value is not None and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return path, text
    return None


def _get_path(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


__all__ = ["IntegrationIdempotencyKey", "build_integration_idempotency_key"]
