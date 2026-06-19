"""Tests for the deliberation store: proposal→debate→decision (BDP-2273 C6, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.deliberation import SqlAlchemyDeliberationStore


def _store(tmp_path) -> SqlAlchemyDeliberationStore:
    return SqlAlchemyDeliberationStore(f"sqlite:///{tmp_path / 'delib.db'}")


def test_start_debate_decide_then_find_decision(tmp_path) -> None:
    store = _store(tmp_path)
    delib = store.start(
        topic="Q3 pricing", proposal="Raise to $99", opened_by="ag_ceo", now=100
    )
    store.add_position(
        deliberation_id=delib.id, agent_id="ag_a", stance="for", body="margins", now=101
    )
    store.add_position(
        deliberation_id=delib.id, agent_id="ag_b", stance="against", body="churn", now=102
    )

    assert len(store.positions(deliberation_id=delib.id)) == 2
    assert store.decide(
        deliberation_id=delib.id, decision="Raise to $89", decided_by="ag_ceo", now=103
    )

    # "What did we decide about Q3 pricing?" is a durable query.
    found = store.find_decision(topic="Q3 pricing")
    assert found is not None
    assert found.decision == "Raise to $89"
    assert found.status == "decided"


def test_decide_is_guarded_only_first_wins(tmp_path) -> None:
    store = _store(tmp_path)
    delib = store.start(topic="t", proposal="p", now=100)

    assert store.decide(deliberation_id=delib.id, decision="x", decided_by="a", now=101)
    # A second decide on an already-decided deliberation does not win the guard.
    assert not store.decide(
        deliberation_id=delib.id, decision="y", decided_by="b", now=102
    )
    assert store.get(deliberation_id=delib.id).decision == "x"


def test_find_decision_returns_none_for_undecided_topic(tmp_path) -> None:
    store = _store(tmp_path)
    store.start(topic="open-topic", proposal="p", now=100)
    assert store.find_decision(topic="open-topic") is None
    assert len(store.list_open()) == 1


def test_positions_are_ordered_by_round(tmp_path) -> None:
    store = _store(tmp_path)
    delib = store.start(topic="t", proposal="p", now=100)
    store.add_position(
        deliberation_id=delib.id, agent_id="a", stance="amend", body="r2", round=2, now=110
    )
    store.add_position(
        deliberation_id=delib.id, agent_id="b", stance="for", body="r1", round=1, now=120
    )
    assert [p.round for p in store.positions(deliberation_id=delib.id)] == [1, 2]
