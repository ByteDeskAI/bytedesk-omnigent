"""Tests for FU1 LLM fact-extraction (BDP-2147 T11, ADR-0132)."""

from __future__ import annotations

import json

from omnigent.runtime.fact_extraction import Fact, capture_facts, parse_facts
from omnigent.stores.memory_store import SqlAlchemyMemoryStore


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")


class _FakeExtractor:
    def __init__(self, raw: str) -> None:
        self._raw = raw

    def extract(self, summary_text: str) -> str:
        return self._raw


def test_parse_facts_validates_and_drops_malformed() -> None:
    raw = json.dumps(
        [
            {"fact": "Ryan chose fastembed.", "salience": 2.0, "confidence": 0.9},
            {"fact": "bad salience", "salience": 1.5, "confidence": 0.9},  # invalid salience
            {"fact": "", "salience": 1.0, "confidence": 0.9},  # empty text
            {"salience": 1.0, "confidence": 0.9},  # no text
            {"fact": "no confidence", "salience": 1.0},  # missing confidence
        ]
    )
    facts = parse_facts(raw)
    assert facts == [Fact(text="Ryan chose fastembed.", salience=2.0, confidence=0.9)]


def test_parse_facts_robust_to_garbage() -> None:
    assert parse_facts("not json") == []
    assert parse_facts("") == []


def test_capture_facts_persists_confident_deduped(tmp_path) -> None:
    store = _store(tmp_path)
    raw = json.dumps(
        [
            {"fact": "Ryan chose fastembed.", "salience": 2.0, "confidence": 0.95},
            {"fact": "ryan chose fastembed.", "salience": 1.0, "confidence": 0.9},  # dup (normalized)
            {"fact": "Low confidence guess.", "salience": 1.0, "confidence": 0.4},  # below threshold
        ]
    )
    n = capture_facts(
        store,
        _FakeExtractor(raw),
        conversation_id="conv_1",
        agent_id="ag_maya",
        summary="…",
        source_compaction_id="cmp_1",
        enabled=True,
    )
    assert n == 1  # one confident, deduped fact
    hits = store.query(scope="agent", owner="ag_maya", name="conv:conv_1:facts", query="fastembed")
    assert len(hits) == 1
    assert hits[0].weight == 2.0  # salience → weight


def test_capture_facts_disabled_by_default(tmp_path) -> None:
    store = _store(tmp_path)
    raw = json.dumps([{"fact": "x", "salience": 1.0, "confidence": 0.9}])
    assert capture_facts(
        store, _FakeExtractor(raw), conversation_id="c", agent_id="ag", summary="s"
    ) == 0  # enabled defaults False


def test_capture_facts_extractor_failure_is_swallowed(tmp_path) -> None:
    store = _store(tmp_path)

    class _Boom:
        def extract(self, summary_text: str) -> str:
            raise RuntimeError("model down")

    assert capture_facts(
        store, _Boom(), conversation_id="c", agent_id="ag", summary="s", enabled=True
    ) == 0
