"""Unit tests for the FU1 omnigent-native memory store (BDP-2147, ADR-0132).

SQLite (the suite's engine) exercises the lexical + weighted-decay +
compartment + sweep logic. The Postgres pgvector semantic blend is verified by
the opt-in integration suite.
"""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory
from omnigent.stores.memory_store import SqlAlchemyMemoryStore

_DAY = 86_400


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'mem.db'}")


def test_append_then_lexical_recall(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(
        scope="agent",
        owner="chief-of-staff",
        name="notes",
        content="Ryan chose in-pod fastembed for omnigent memory.",
    )
    hits = store.query(
        scope="agent", owner="chief-of-staff", name="notes", query="fastembed"
    )
    assert len(hits) == 1
    assert "fastembed" in hits[0].content


def test_query_is_compartment_scoped(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(scope="agent", owner="maya", name="notes", content="alpha secret")
    store.append(scope="agent", owner="nolan", name="notes", content="alpha secret")
    maya_hits = store.query(scope="agent", owner="maya", name="notes", query="alpha")
    assert len(maya_hits) == 1
    # An unknown compartment yields nothing (no cross-owner leakage).
    assert store.query(scope="agent", owner="ghost", name="notes", query="alpha") == []


def test_decay_drops_stale_below_read_floor(tmp_path) -> None:
    """A short-half-life memory falls off recall at read time once decayed —
    the pure-read floor, independent of the sweep."""
    store = _store(tmp_path)
    base = int(time.time())
    store.append(
        scope="topic",
        owner="bytedesk",
        name="ephemeral",
        content="transient detail about the build",
        half_life_seconds=100,
        now=base,
    )
    # Fresh: effective weight ~1.0 >= read_floor 0.1 -> recalled.
    assert (
        len(
            store.query(
                scope="topic", owner="bytedesk", name="ephemeral", query="transient", now=base
            )
        )
        == 1
    )
    # Aged ~10 half-lives: exp(-10) ~ 4.5e-5 < 0.1 -> dropped, no sweep needed.
    assert (
        store.query(
            scope="topic",
            owner="bytedesk",
            name="ephemeral",
            query="transient",
            now=base + 1000,
        )
        == []
    )


def test_query_is_a_pure_read(tmp_path) -> None:
    """Recall must not mutate ``last_accessed_at`` / ``access_count`` —
    reinforcement is out-of-band (T8)."""
    store = _store(tmp_path)
    base = int(time.time())
    mid = store.append(
        scope="agent", owner="maya", name="notes", content="durable fact xyz", now=base
    )

    store.query(scope="agent", owner="maya", name="notes", query="durable", now=base + 5)
    store.query(scope="agent", owner="maya", name="notes", query="durable", now=base + 9)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mem.db'}")
    with Session(engine) as session:
        row = session.get(SqlMemory, mid)
        assert row is not None
        assert row.last_accessed_at == base, "query mutated last_accessed_at"
        assert row.access_count == 0, "query mutated access_count"


def test_sweep_archives_decayed_and_excludes_from_recall(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    store.append(
        scope="topic",
        owner="bytedesk",
        name="ephemeral",
        content="stale build artifact note",
        half_life_seconds=100,
        archive_floor=0.05,
        now=base,
    )
    later = base + 40 * _DAY  # past the 30-day grace, fully decayed
    archived = store.sweep(now=later)
    assert archived == 1
    assert (
        store.query(
            scope="topic", owner="bytedesk", name="ephemeral", query="stale", now=later
        )
        == []
    )


def test_sweep_spares_fresh_memories(tmp_path) -> None:
    store = _store(tmp_path)
    base = int(time.time())
    store.append(
        scope="topic",
        owner="bytedesk",
        name="ephemeral",
        content="recent relevant note",
        half_life_seconds=100,
        now=base,
    )
    # Same instant: within grace -> not archived even though it would decay.
    assert store.sweep(now=base) == 0


def test_list_compartments(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(scope="agent", owner="maya", name="notes", content="a")
    store.append(scope="team", owner="leadership", name="roster", content="b")
    all_comps = store.list_compartments()
    assert {(c["scope"], c["owner"], c["name"]) for c in all_comps} == {
        ("agent", "maya", "notes"),
        ("team", "leadership", "roster"),
    }
    agent_only = store.list_compartments(scope="agent")
    assert len(agent_only) == 1 and agent_only[0]["owner"] == "maya"


def test_invalid_scope_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="invalid memory scope"):
        store.append(scope="tenant", owner="acme", name="facts", content="x")
