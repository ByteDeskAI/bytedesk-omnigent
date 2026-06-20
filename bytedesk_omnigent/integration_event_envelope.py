"""Deterministic connected-app ingress event envelopes.

Webhook adapters verify source-specific wire contracts; this module gives the
runtime a small, stable payload shape that agents and workflow harnesses can
consume without learning every provider's headers or signature scheme.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

SCHEMA = "omnigent.integration_event.v1"

_DELIVERY_HEADERS = (
    "x-github-delivery",
    "x-request-id",
    "x-slack-request-timestamp",
    "x-linear-delivery",
    "x-atlassian-webhook-identifier",
    "stripe-signature",  # timestamp/signature header; do not echo full value.
)
_HOOK_HEADERS = (
    "x-github-hook-id",
    "x-github-hook-installation-target-id",
    "x-hook-id",
)
_CONTENT_TYPE = "content-type"


@dataclass(frozen=True)
class IntegrationEventEnvelope:
    """Provider-neutral event context delivered to an agent or workflow harness."""

    schema: str
    source: str
    event: str
    received_at: int
    payload: dict[str, object]
    metadata: dict[str, str]

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-serializable dict for signal-bus delivery or task payloads."""
        return {
            "schema": self.schema,
            "source": self.source,
            "event": self.event,
            "received_at": self.received_at,
            "payload": self.payload,
            "metadata": self.metadata,
        }


def build_integration_event_envelope(
    *,
    source: str,
    match_key: str,
    payload: Mapping[str, object] | None,
    headers: Mapping[str, str],
    received_at: int,
) -> IntegrationEventEnvelope:
    """Build a sanitized, deterministic event envelope.

    Only non-secret correlation metadata is copied out of headers. Signature,
    authorization, cookie, and token headers stay at the ingress boundary.
    """
    return IntegrationEventEnvelope(
        schema=SCHEMA,
        source=_slug(source),
        event=match_key or "*",
        received_at=received_at,
        payload=dict(payload or {}),
        metadata=_safe_metadata(headers),
    )


def _safe_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    content_type = _header(headers, _CONTENT_TYPE)
    if content_type:
        metadata["content_type"] = content_type
    delivery_id = _first_header(headers, _DELIVERY_HEADERS)
    if delivery_id:
        # Stripe-Signature includes the signature itself; retain only that a
        # provider timestamp exists if Stripe is the only available correlation.
        metadata["delivery_id"] = (
            "stripe-signature-present" if ",v1=" in delivery_id else delivery_id
        )
    hook_id = _first_header(headers, _HOOK_HEADERS)
    if hook_id:
        metadata["hook_id"] = hook_id
    return metadata


def _first_header(headers: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = _header(headers, name)
        if value:
            return value
    return ""


def _header(headers: Mapping[str, str], name: str) -> str:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"
