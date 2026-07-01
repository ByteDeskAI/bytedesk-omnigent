"""Verify schema optimization migration indexes and foreign keys."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.engine import Engine

from omnigent.db.db_models import SqlConversationItem
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVIDENCE_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "verify_schema_opt_evidence.py"


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


def _load_evidence_module():
    spec = importlib.util.spec_from_file_location(
        "verify_schema_opt_evidence", _EVIDENCE_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_opt_inspector_transcript(db_engine: Engine) -> None:
    """Build the same transcript the standalone evidence script writes."""
    evidence = _load_evidence_module()
    transcript = evidence.build_schema_opt_transcript(db_engine)
    assert transcript.startswith("schema_opt inspector transcript")
    assert "ix_conversations_runner_id" in transcript
    assert "ix_conversations_agent_id" in transcript
    assert "ix_conversations_active_sessions" in transcript
    assert "fk_comments_conversation_id" in transcript
    assert "redundant_indexes_absent=True" in transcript

    scratch_dir = os.environ.get("SCHEMA_OPT_SCRATCH_DIR")
    if scratch_dir:
        out = Path(scratch_dir) / "inspector-transcript.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(transcript + "\n", encoding="utf-8")


def test_verify_schema_opt_evidence_script_writes_transcript(tmp_path: Path) -> None:
    """Standalone script captures get_or_create_engine + inspector evidence."""
    out = tmp_path / "inspector-transcript.log"
    pg_skip = tmp_path / "pg-skip.log"
    result = subprocess.run(
        [
            sys.executable,
            str(_EVIDENCE_SCRIPT),
            "--output",
            str(out),
            "--pg-skip-output",
            str(pg_skip),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = out.read_text(encoding="utf-8")
    assert "schema_opt inspector transcript" in body
    assert "ix_conversations_runner_id" in body
    assert "fk_comments_conversation_id" in body
    assert "schema_opt inspector transcript" in result.stdout
    assert pg_skip.exists()


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


def _upgrade_with_orphan_rows(
    tmp_path: Path,
    *,
    db_name: str,
    seed_statements: list[str],
    orphan_id: str,
    orphan_table: str,
    fk_table: str,
    fk_name: str,
) -> None:
    db_path = tmp_path / db_name
    uri = f"sqlite:///{db_path}"
    engine = _raw_migration_engine(uri)
    try:
        _alembic_to(engine, uri, "bdp2609wftools")
        now = 1_700_000_000
        params = {"ts": now, "ts_us": now * 1_000_000}
        with engine.begin() as conn:
            for stmt in seed_statements:
                conn.execute(sa.text(stmt), params)
            orphan_count = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM {orphan_table} WHERE id = :oid"),
                {"oid": orphan_id},
            ).scalar_one()
            assert orphan_count == 1

        _alembic_to(engine, uri, "head")

        with engine.connect() as conn:
            remaining = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM {orphan_table} WHERE id = :oid"),
                {"oid": orphan_id},
            ).scalar_one()
            assert remaining == 0
            fk_names = {fk["name"] for fk in sa.inspect(conn).get_foreign_keys(fk_table)}
            assert fk_name in fk_names
    finally:
        engine.dispose()
        clear_engine_cache()


def test_bdp2610_deletes_orphan_comments_before_fk(tmp_path: Path) -> None:
    """Orphan comment rows are removed during upgrade so FK creation succeeds."""
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_comments.db",
        seed_statements=[
            (
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind) "
                "VALUES ('conv_keep', :ts, :ts, 'conv_keep', 'default')"
            ),
            (
                "INSERT INTO comments "
                "(id, conversation_id, path, start_index, end_index, body, status, "
                "created_at, updated_at) "
                "VALUES ('cmt_orphan', 'conv_missing', 'a.ts', 0, 1, 'x', 'draft', "
                ":ts, :ts_us)"
            ),
        ],
        orphan_id="cmt_orphan",
        orphan_table="comments",
        fk_table="comments",
        fk_name="fk_comments_conversation_id",
    )


def test_bdp2610_deletes_orphan_goal_dependencies_before_fk(tmp_path: Path) -> None:
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_goal_deps.db",
        seed_statements=[
            (
                "INSERT INTO goal_dependencies "
                "(id, goal_id, kind, label, created_at, updated_at) "
                "VALUES ('dep_orphan', 'goal_missing', 'manual', 'orphan', :ts, :ts)"
            ),
        ],
        orphan_id="dep_orphan",
        orphan_table="goal_dependencies",
        fk_table="goal_dependencies",
        fk_name="fk_goal_dependencies_goal_id",
    )


def test_bdp2610_deletes_orphan_goal_outcomes_before_fk(tmp_path: Path) -> None:
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_goal_outcomes.db",
        seed_statements=[
            (
                "INSERT INTO goal_outcomes "
                "(id, goal_id, booked_at, realized_value_cents, source) "
                "VALUES ('out_orphan', 'goal_missing', :ts, 0, 'test')"
            ),
        ],
        orphan_id="out_orphan",
        orphan_table="goal_outcomes",
        fk_table="goal_outcomes",
        fk_name="fk_goal_outcomes_goal_id",
    )


def test_bdp2610_deletes_orphan_inbound_event_results_before_fk(tmp_path: Path) -> None:
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_inbound_results.db",
        seed_statements=[
            (
                "INSERT INTO inbound_event_results "
                "(id, idempotency_key, processor, created_at, updated_at) "
                "VALUES ('ier_orphan', 'evt_missing', 'router', :ts, :ts)"
            ),
        ],
        orphan_id="ier_orphan",
        orphan_table="inbound_event_results",
        fk_table="inbound_event_results",
        fk_name="fk_inbound_event_results_idempotency_key",
    )


def test_bdp2610_deletes_orphan_connector_services_before_fk(tmp_path: Path) -> None:
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_connector_services.db",
        seed_statements=[
            (
                "INSERT INTO connector_services "
                "(id, connection_id, service_key, updated_at) "
                "VALUES ('svc_orphan', 'conn_missing', 'gmail', :ts)"
            ),
        ],
        orphan_id="svc_orphan",
        orphan_table="connector_services",
        fk_table="connector_services",
        fk_name="fk_connector_services_connection_id",
    )


def test_bdp2610_deletes_orphan_connector_grants_before_fk(tmp_path: Path) -> None:
    _upgrade_with_orphan_rows(
        tmp_path,
        db_name="orphan_connector_grants.db",
        seed_statements=[
            (
                "INSERT INTO connector_agent_grants "
                "(id, connection_id, agent_id, service_key, tool_key, created_at, updated_at) "
                "VALUES ('grant_orphan', 'conn_missing', 'ag_test', 'gmail', 'send', :ts, :ts)"
            ),
        ],
        orphan_id="grant_orphan",
        orphan_table="connector_agent_grants",
        fk_table="connector_agent_grants",
        fk_name="fk_connector_agent_grants_connection_id",
    )