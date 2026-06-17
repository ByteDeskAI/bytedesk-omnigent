"""LLM fact-extraction at the compaction boundary (FU1 T11, ADR-0132).

A second, opt-in pass distills a Layer-2 compaction summary into discrete,
weighted facts written to durable memory — so recall surfaces atomic facts, not
just a prose summary. The LLM call is a **pluggable** ``FactExtractor`` (the
real model-backed impl is wired at the T14 in-cluster phase; tests inject a
fake), keeping this module dependency-light and the upstream surface minimal.

Guardrails (avoid hallucinated facts polluting memory): drop facts below a
confidence threshold; dedup by normalized text; carry provenance
(``source_compaction_id``) so a fact traces back to its summary; map salience
to weight on a fixed scale so the model never invents free-form weights. Errors
are logged (never silently swallowed); extraction is off by default
(``enabled=False``) until recall observability proves it safe to enable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

_logger = logging.getLogger(__name__)

# Fixed salience → weight scale (ADR-0132): the model picks a level, not a float.
_VALID_SALIENCE = (0.3, 1.0, 2.0, 3.0)
_DEFAULT_CONFIDENCE_THRESHOLD = 0.7
_FACT_HALF_LIFE_SECONDS = 30 * 86_400


@dataclass(frozen=True)
class Fact:
    """A distilled fact with salience (→ weight) and extraction confidence."""

    text: str
    salience: float
    confidence: float


class FactExtractor(Protocol):
    """Pluggable summary→facts backend (an LLM call in production)."""

    def extract(self, summary_text: str) -> str:
        """Return raw JSON: a list of {fact, salience, confidence} objects."""
        ...


def parse_facts(raw: str) -> list[Fact]:
    """Parse the extractor's raw JSON into validated :class:`Fact` objects.

    Robust to malformed output — anything that doesn't validate is dropped
    (a bad extraction must never crash the turn or poison memory).
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("facts", [])
    if not isinstance(payload, list):
        return []
    facts: list[Fact] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        text = entry.get("fact") or entry.get("text")
        salience = entry.get("salience")
        confidence = entry.get("confidence")
        if not isinstance(text, str) or not text.strip():
            continue
        if salience not in _VALID_SALIENCE:
            continue
        if not isinstance(confidence, (int, float)):
            continue
        facts.append(Fact(text=text.strip(), salience=float(salience), confidence=float(confidence)))
    return facts


def capture_facts(
    store,
    extractor: FactExtractor,
    *,
    conversation_id: str,
    agent_id: str | None,
    summary: str,
    source_compaction_id: str | None = None,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    enabled: bool = False,
) -> int:
    """Extract facts from *summary* and persist the confident, deduped ones.

    :returns: The number of facts persisted (0 when disabled / no agent / on
        any extraction failure).
    """
    if not enabled or not agent_id or not summary:
        return 0
    try:
        raw = extractor.extract(summary)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "fact extraction failed for %s/%s", conversation_id, source_compaction_id, exc_info=True
        )
        return 0

    facts = [f for f in parse_facts(raw) if f.confidence >= confidence_threshold]
    persisted = 0
    seen: set[str] = set()
    for fact in facts:
        key = fact.text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            store.append(
                scope="agent",
                owner=agent_id,
                name=f"conv:{conversation_id}:facts",
                content=fact.text,
                weight=fact.salience,
                half_life_seconds=_FACT_HALF_LIFE_SECONDS,
                salience=fact.salience,
                confidence=fact.confidence,
                source_conversation_id=conversation_id,
                source_compaction_id=source_compaction_id,
            )
            persisted += 1
        except Exception:  # noqa: BLE001
            _logger.warning(
                "fact persist failed for %s: %r", conversation_id, fact.text[:80], exc_info=True
            )
    return persisted
