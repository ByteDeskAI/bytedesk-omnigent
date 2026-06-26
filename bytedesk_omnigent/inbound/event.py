"""Canonical inbound-event envelope (ADR-0155, BDP-2558).

The **Canonical Data Model** (EIP) for everything that flows into Omnigent from an
external source — webhooks (GitHub, Jira), email, raw signal deliveries. One
normalized envelope so the pipeline (translate → wire-tap → idempotent claim →
fan-out) and the durable ``inbound_events`` log speak one language regardless of
source. The ``idempotency_key`` is the single dedupe axis that replaces the three
divergent per-consumer schemes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def select_headers(headers: Mapping[str, str], allow: tuple[str, ...]) -> dict[str, str]:
    """Whitelist a subset of headers (case-insensitive). Never store the signature."""
    lower = {k.lower(): v for k, v in headers.items()}
    return {name: lower[name] for name in allow if name in lower}


def body_fingerprint(raw_payload: dict[str, Any]) -> str:
    """A stable content hash for idempotency keys that lack a native delivery id."""
    encoded = json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class InboundEvent:
    """One normalized inbound event (Canonical Data Model)."""

    idempotency_key: str  # the single dedupe axis (Idempotent Receiver PK)
    source: str  # "github" | "jira" | "agentic-inbox" | ...
    type: str  # canonical verb: "pull_request.merged" | "jira.issue_updated" | "email.received" | "signal.deliver"
    occurred_at: int  # epoch secs from the source when available, else received_at
    received_at: int  # epoch secs when WE ingested
    raw_payload: dict[str, Any]  # verbatim body (the Wire-Tap log keeps this)
    normalized: dict[str, Any] = field(default_factory=dict)  # translator-extracted fields
    headers: dict[str, str] = field(default_factory=dict)  # whitelisted subset (no signature)
    tenant_id: str | None = None
    event_id: str | None = None  # source-native id when distinct from idempotency_key
