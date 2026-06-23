"""Tests for the config-store ``version`` column migration (z5, BDP-2412).

Asserts the single revision adds a NOT NULL ``version`` to all five writable
config stores, backfills pre-existing rows to 1 (server_default), and is
reversible.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_TABLES = ("policies", "conversations", "session_permissions", "cron_triggers", "webhook_bindings")


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_version_column_added_to_all_config_stores(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    for table in _TABLES:
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        assert "version" in cols, f"{table}.version missing — migration did not add it"
        assert not cols["version"]["nullable"], f"{table}.version must be NOT NULL"


def test_version_backfills_existing_rows_to_one(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'backfill.db'}"
    engine = get_or_create_engine(uri)
    cfg = _build_alembic_config(uri)
    try:
        # Roll back the version column, insert a pre-existing row, then re-apply:
        # the NOT NULL add against a populated table must backfill to 1.
        command.downgrade(cfg, "z4a2b3c4d5e6")
        with engine.begin() as conn:
            assert "version" not in {
                c["name"] for c in sa.inspect(engine).get_columns("conversations")
            }
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, root_conversation_id, kind) "
                    "VALUES ('conv_bf', 1, 1, 'conv_bf', 'default')"
                )
            )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            version = conn.execute(
                sa.text("SELECT version FROM conversations WHERE id = 'conv_bf'")
            ).scalar()
        assert version == 1
    finally:
        clear_engine_cache()


def test_downgrade_drops_version(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'down.db'}"
    engine = get_or_create_engine(uri)
    cfg = _build_alembic_config(uri)
    try:
        command.downgrade(cfg, "z4a2b3c4d5e6")
        for table in _TABLES:
            cols = {c["name"] for c in sa.inspect(engine).get_columns(table)}
            assert "version" not in cols, f"downgrade left {table}.version"
    finally:
        clear_engine_cache()
