"""Database contract after the NATS AgentStore cutover."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.db_models import SqlConversation
from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_conversation_agent_id_is_external_reference_in_model() -> None:
    agent_fks = [
        fk
        for fk in SqlConversation.__table__.foreign_keys
        if fk.parent.name == "agent_id" and fk.column.table.name == "agents"
    ]

    assert agent_fks == []


def test_conversation_agent_id_has_no_sql_agent_fk_after_migration(
    db_engine: Engine,
) -> None:
    fks = sa.inspect(db_engine).get_foreign_keys("conversations")
    agent_fks = [
        fk for fk in fks if "agent_id" in fk.get("constrained_columns", [])
    ]

    assert agent_fks == []


def test_conversation_can_reference_agent_not_in_sql_agents(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind, agent_id) "
                "VALUES (:id, :ts, :ts, :id, 'default', :agent_id)"
            ),
            {"id": "conv_external_agent", "ts": 1700000000, "agent_id": "ag_nats_only"},
        )
        result = conn.execute(
            sa.text("SELECT agent_id FROM conversations WHERE id = :id"),
            {"id": "conv_external_agent"},
        ).scalar_one()
        conn.commit()

    assert result == "ag_nats_only"
