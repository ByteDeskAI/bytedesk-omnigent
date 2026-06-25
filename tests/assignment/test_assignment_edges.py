"""Edge tests for assignment resolver helpers."""

from __future__ import annotations

import pytest

from bytedesk_omnigent.assignment import _candidate, _default_scoreboard
from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.outcomes import SqlAlchemyOutcomeLedger


def test_candidate_raises_when_entry_has_no_identity() -> None:
    class _NoId:
        department = "eng"
        capabilities = ("dotnet",)

    with pytest.raises(ValueError, match="agent_id / name"):
        _candidate(_NoId())


def test_candidate_coerces_name_when_agent_id_absent() -> None:
    class _Named:
        name = "from-name"
        department = "eng"
        capabilities = ["dotnet"]

    cand = _candidate(_Named())
    assert cand.agent_id == "from-name"
    assert cand.capabilities == ("dotnet",)


def test_default_scoreboard_reads_goal_store(tmp_path, monkeypatch) -> None:
    db = f"sqlite:///{tmp_path / 'org.db'}"
    ledger = SqlAlchemyOutcomeLedger(db)
    goals = SqlAlchemyGoalStore(db)
    ledger.record_outcome(agent_id="elias", kind="feature_shipped", metric="ships", value=3, now=1)
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: goals)

    rows = _default_scoreboard("ships")
    assert rows == [("elias", 3.0)]
