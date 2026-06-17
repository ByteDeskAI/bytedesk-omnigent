"""Schema tests for the FU1 omnigent-native memory tables (BDP-2147, ADR-0132).

Exercises the dialect-aware migration on SQLite (the suite's engine): the two
tables, their key columns, and the recall index exist, and a compartment +
memory round-trip through the ORM. The PostgreSQL ``vector(384)`` / ivfflat
path is guarded in the migration and verified by the opt-in integration suite.
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemory, SqlMemoryCompartment
from omnigent.db.utils import get_or_create_engine

_MEMORY_COLS = {
    "id",
    "compartment_id",
    "content",
    "search_text",
    "weight",
    "created_at",
    "last_accessed_at",
    "access_count",
    "source_conversation_id",
    "source_compaction_id",
    "salience",
    "confidence",
    "archived",
    "embedding",
    "embedding_model_version",
    "metadata",
}


def test_memory_tables_and_indexes_created(tmp_path) -> None:
    """The migration creates both memory tables, the memories columns, and the
    composite recall index on a fresh SQLite DB."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'mem.db'}")
    inspector = sa.inspect(engine)

    tables = set(inspector.get_table_names())
    assert {"memory_compartments", "memories"} <= tables, sorted(tables)

    cols = {c["name"] for c in inspector.get_columns("memories")}
    assert _MEMORY_COLS <= cols, sorted(_MEMORY_COLS - cols)

    comp_cols = {c["name"] for c in inspector.get_columns("memory_compartments")}
    assert {"scope", "owner", "name", "half_life_seconds", "read_floor", "archive_floor"} <= comp_cols

    index_names = {i["name"] for i in inspector.get_indexes("memories")}
    assert "ix_memories_compartment_archived_weight" in index_names, sorted(index_names)


def test_memory_roundtrips_through_orm(tmp_path) -> None:
    """A compartment and a memory persist and read back with their fields,
    including the ``meta`` -> ``metadata`` mapping and defaults."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'mem.db'}")
    now = int(time.time())

    # Mirror MemoryStore.append: the compartment is get-or-created (and
    # committed) before the memory that references it — SQLite FK enforcement
    # is ON, so the parent must exist first.
    with Session(engine) as session:
        session.add(
            SqlMemoryCompartment(
                id="mc_notes",
                scope="agent",
                owner="chief-of-staff",
                name="notes",
                half_life_seconds=1_209_600,
                created_at=now,
            )
        )
        session.commit()

    with Session(engine) as session:
        session.add(
            SqlMemory(
                id="mem_1",
                compartment_id="mc_notes",
                content="Ryan chose in-pod fastembed for omnigent memory.",
                search_text="ryan chose in-pod fastembed for omnigent memory",
                weight=2.0,
                created_at=now,
                last_accessed_at=now,
            )
        )
        session.commit()

    with Session(engine) as session:
        mem = session.get(SqlMemory, "mem_1")
        assert mem is not None
        assert mem.compartment_id == "mc_notes"
        assert mem.weight == 2.0
        assert mem.access_count == 0
        assert mem.archived is False
        assert mem.meta is None
        assert mem.embedding is None

        comp = session.get(SqlMemoryCompartment, "mc_notes")
        assert comp is not None
        assert comp.read_floor == 0.1
        assert comp.archive_floor == 0.05
