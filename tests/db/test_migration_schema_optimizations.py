"""Verify schema optimization migration indexes and foreign keys."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


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