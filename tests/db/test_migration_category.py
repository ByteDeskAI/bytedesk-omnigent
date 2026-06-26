"""Tests for the ``agents.category`` migration (``bdpagentcategory``).

Adds the agent-tier classification column (agent-tiering step 1). The deterministic
backfill must set allowlisted system-agent ids to ``"system"`` and every other
existing row to ``"employee"``; ``workflow`` is resolved later by the startup seed
(SQL can't read the bundle params). Downgrade must drop the column + index.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, builtin_agent_id

_PRIOR_HEAD = "bdp2557inbound"
_THIS_REVISION = "bdpagentcategory"


def _new_engine(uri: str) -> sa.Engine:
    engine = sa.create_engine(uri)
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
    return engine


def _upgrade(engine: sa.Engine, uri: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(engine: sa.Engine, uri: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def _insert_agent(engine: sa.Engine, *, agent_id: str, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, created_at, name, bundle_location, version) "
                "VALUES (:id, :ts, :name, :loc, 1)"
            ),
            {"id": agent_id, "ts": 1_700_000_000, "name": name, "loc": f"{agent_id}/h"},
        )


def test_category_backfill_system_and_employee(tmp_path: Path) -> None:
    """Allowlisted ids → 'system'; all other existing rows → 'employee'."""
    uri = f"sqlite:///{tmp_path / 'agent-category.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, _PRIOR_HEAD)
        sys_id = builtin_agent_id("polly")
        _insert_agent(engine, agent_id=sys_id, name="polly")
        _insert_agent(engine, agent_id="ag_employee", name="vivian")

        _upgrade(engine, uri, _THIS_REVISION)

        with engine.connect() as conn:
            rows = {
                str(r["id"]): r["category"]
                for r in conn.execute(
                    sa.text("SELECT id, category FROM agents")
                ).mappings()
            }
        assert rows[sys_id] == "system"
        assert rows["ag_employee"] == "employee"
    finally:
        engine.dispose()


def test_category_column_and_index_present_after_upgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'agent-category-cols.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, _THIS_REVISION)
        inspector = sa.inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("agents")}
        assert "category" in cols
        indexes = {ix["name"] for ix in inspector.get_indexes("agents")}
        assert "ix_agents_category" in indexes
    finally:
        engine.dispose()


def test_downgrade_drops_category(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'agent-category-down.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, _THIS_REVISION)
        _downgrade(engine, uri, _PRIOR_HEAD)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("agents")}
        assert "category" not in cols
    finally:
        engine.dispose()
