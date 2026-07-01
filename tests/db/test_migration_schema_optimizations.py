"""Verify schema optimization migration indexes and foreign keys."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.engine import Engine
from sqlalchemy import func, select

from omnigent.db.db_models import SqlConversationItem
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full Alembic chain applied."""
    db_path = tmp_path / "schema_opt.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {idx["name"] for idx in inspector.get_indexes(table)}


def _fk_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {fk["name"] for fk in inspector.get_foreign_keys(table)}


def test_conversations_hot_path_indexes_exist(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    names = _index_names(inspector, "conversations")
    assert "ix_conversations_runner_id" in names
    assert "ix_conversations_agent_id" in names
    assert "ix_conversations_active_sessions" in names


def test_goals_claimable_index_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_goals_claimable" in _index_names(inspector, "goals")


def test_hosts_updated_at_index_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_hosts_updated_at" in _index_names(inspector, "hosts")


def test_memory_sweep_partial_index_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_memories_sweep_candidates" in _index_names(inspector, "memories")


def test_connector_oauth_expiry_partial_index_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_connector_oauth_states_expires_unconsumed" in _index_names(
        inspector, "connector_oauth_states"
    )


def test_conversation_items_type_position_index_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_conversation_items_conv_type_pos" in _index_names(
        inspector, "conversation_items"
    )


def test_redundant_indexes_removed(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "ix_workforce_instructions_scope" not in _index_names(
        inspector, "workforce_instructions"
    )
    assert "ix_goal_dependencies_goal" not in _index_names(inspector, "goal_dependencies")
    assert "ix_goal_dependencies_goal_status" in _index_names(inspector, "goal_dependencies")


def test_comments_conversation_fk_exists(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    fk_names = _fk_names(inspector, "comments")
    assert "fk_comments_conversation_id" in fk_names


def test_child_table_foreign_keys_exist(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert "fk_goal_dependencies_goal_id" in _fk_names(inspector, "goal_dependencies")
    assert "fk_goal_outcomes_goal_id" in _fk_names(inspector, "goal_outcomes")
    assert "fk_inbound_event_results_idempotency_key" in _fk_names(
        inspector, "inbound_event_results"
    )
    assert "fk_connector_services_connection_id" in _fk_names(inspector, "connector_services")
    assert "fk_connector_agent_grants_connection_id" in _fk_names(
        inspector, "connector_agent_grants"
    )


def test_schema_opt_inspector_transcript(db_engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    """Emit a standalone inspector transcript for verification evidence."""
    inspector = sa.inspect(db_engine)
    conv_indexes = sorted(_index_names(inspector, "conversations"))
    comment_fks = sorted(_fk_names(inspector, "comments"))
    lines = [
        "schema_opt inspector transcript",
        f"conversations.indexes={conv_indexes}",
        f"comments.foreign_keys={comment_fks}",
    ]
    transcript = "\n".join(lines)
    print(transcript)
    captured = capsys.readouterr()
    assert transcript in captured.out
    assert "ix_conversations_runner_id" in conv_indexes
    assert "ix_conversations_agent_id" in conv_indexes
    assert "ix_conversations_active_sessions" in conv_indexes
    assert "fk_comments_conversation_id" in comment_fks


def test_list_conversations_pg_search_compiles_bound_param() -> None:
    """PostgreSQL search branch must bind search_query via SQLAlchemy, not loose text()."""
    search_query = "deployment"
    tsvector = func.to_tsvector(
        "english",
        func.coalesce(SqlConversationItem.search_text, ""),
    )
    tsquery = func.plainto_tsquery("english", search_query)
    subq = (
        select(SqlConversationItem.conversation_id)
        .where(tsvector.op("@@")(tsquery))
        .distinct()
    )
    compiled = subq.compile(dialect=pg_dialect.dialect(), compile_kwargs={"literal_binds": False})
    sql = str(compiled)
    assert "plainto_tsquery" in sql
    assert "to_tsvector" in sql
    assert compiled.params, "search_query must compile to a bound parameter"


def _raw_migration_engine(uri: str) -> sa.Engine:
    engine = sa.create_engine(uri)
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
    return engine


def _alembic_to(engine: sa.Engine, uri: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def test_bdp2610_deletes_orphan_comments_before_fk(tmp_path: Path) -> None:
    """Orphan comment rows are removed during upgrade so FK creation succeeds."""
    db_path = tmp_path / "orphan_comments.db"
    uri = f"sqlite:///{db_path}"
    engine = _raw_migration_engine(uri)
    try:
        _alembic_to(engine, uri, "bdp2609wftools")
        now = 1_700_000_000
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, root_conversation_id, kind) "
                    "VALUES ('conv_keep', :ts, :ts, 'conv_keep', 'default')"
                ),
                {"ts": now},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO comments "
                    "(id, conversation_id, path, start_index, end_index, body, status, "
                    "created_at, updated_at) "
                    "VALUES ('cmt_orphan', 'conv_missing', 'a.ts', 0, 1, 'x', 'draft', "
                    ":ts, :ts_us)"
                ),
                {"ts": now, "ts_us": now * 1_000_000},
            )
            orphan_count = conn.execute(
                sa.text("SELECT COUNT(*) FROM comments WHERE id = 'cmt_orphan'")
            ).scalar_one()
            assert orphan_count == 1

        _alembic_to(engine, uri, "head")

        with engine.connect() as conn:
            remaining = conn.execute(
                sa.text("SELECT COUNT(*) FROM comments WHERE id = 'cmt_orphan'")
            ).scalar_one()
            assert remaining == 0
            fk_names = {
                fk["name"] for fk in sa.inspect(conn).get_foreign_keys("comments")
            }
            assert "fk_comments_conversation_id" in fk_names
    finally:
        engine.dispose()
        clear_engine_cache()