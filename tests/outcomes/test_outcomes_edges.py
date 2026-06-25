"""Edge tests for outcome ledger engine, cache accessor, and filters."""

from __future__ import annotations

from dataclasses import dataclass

from bytedesk_omnigent.outcomes import SqlAlchemyOutcomeLedger, get_outcome_ledger


def test_engine_property_exposes_underlying_engine(tmp_path) -> None:
    ledger = SqlAlchemyOutcomeLedger(f"sqlite:///{tmp_path / 'out.db'}")
    assert ledger.engine is not None


def test_list_outcomes_filters_by_agent(tmp_path) -> None:
    ledger = SqlAlchemyOutcomeLedger(f"sqlite:///{tmp_path / 'out.db'}")
    ledger.record_outcome(agent_id="ag_a", kind="deal_won", metric="revenue", value=1, now=1)
    ledger.record_outcome(agent_id="ag_b", kind="deal_won", metric="revenue", value=2, now=2)
    assert len(ledger.list_outcomes(agent_id="ag_a")) == 1
    assert ledger.list_outcomes(agent_id="ag_a")[0].agent_id == "ag_a"


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_outcome_ledger_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_outcome_ledger.__globals__["_outcome_ledger_cache"].clear()

    first = get_outcome_ledger()
    second = get_outcome_ledger()
    assert first is second
    assert isinstance(first, SqlAlchemyOutcomeLedger)
