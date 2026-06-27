"""Canonical inbound ingress translator (Phase 4 lights up ADR-0155 P8).

The connected-app provider path posts an ALREADY-canonical event (the app, not the
engine, did the domain translation — the engine stays domain-blind). So the
pipeline's translate step is an **identity passthrough**: build an
:class:`InboundEvent` from the posted canonical fields and hand it to the same
``pipeline.ingest`` chain (wire-tap → idempotent claim → fan-out) every other
channel uses. No parallel pipeline.
"""
from __future__ import annotations

from collections.abc import Mapping

from bytedesk_omnigent.inbound.event import InboundEvent, body_fingerprint

CHANNEL_PROVIDER = "provider"


class CanonicalTranslator:
    """Identity translator: a posted canonical body → :class:`InboundEvent`.

    Required fields: ``type``. ``idempotency_key`` defaults to
    ``provider:{source}:{type}:{body-fingerprint}`` when the app does not supply one
    (the pipeline's Idempotent Receiver still dedupes on it).
    """

    def translate(
        self,
        *,
        source: str,
        raw_payload: dict,
        headers: Mapping[str, str],  # noqa: ARG002 - Protocol signature; app pre-translates
        now: int,
    ) -> InboundEvent | None:
        event_type = raw_payload.get("type")
        if not event_type:
            return None
        normalized = raw_payload.get("normalized") or {}
        body = raw_payload.get("raw_payload") or raw_payload
        key = raw_payload.get("idempotency_key") or (
            f"provider:{source}:{event_type}:{body_fingerprint(body)}"
        )
        return InboundEvent(
            idempotency_key=str(key),
            source=source,
            type=str(event_type),
            occurred_at=int(raw_payload.get("occurred_at", now)),
            received_at=now,
            raw_payload=body,
            normalized=normalized,
            headers={},
            tenant_id=raw_payload.get("tenant_id"),
            event_id=raw_payload.get("event_id"),
        )


def register_canonical_translator() -> None:
    """Register the canonical passthrough translator for the ``provider`` channel."""
    from bytedesk_omnigent.inbound.translators import register_translator

    register_translator(CHANNEL_PROVIDER, CanonicalTranslator)


def register_outcome_processor() -> None:
    """Register the OutcomeSource sink processor (provider-pushed value → treasury)."""
    from bytedesk_omnigent.engine.providers.outcome import OutcomeProcessor
    from bytedesk_omnigent.inbound.processors import register_processor

    register_processor("outcome-source", OutcomeProcessor)


__all__ = [
    "CHANNEL_PROVIDER",
    "CanonicalTranslator",
    "register_canonical_translator",
    "register_outcome_processor",
]
