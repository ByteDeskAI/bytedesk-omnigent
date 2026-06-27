"""Tests for the ``goals`` cadence columns + their migration (BDP-2583).

Cadence drives how often a goal is dispatched to its agent. ``cadence_kind`` is
NOT NULL with a ``server_default`` of ``immediate`` so existing goals backfill to
the unchanged once-when-ready behaviour; ``cadence_expr`` / ``cadence_tz`` are
nullable (only recurring/until_done goals carry a cron expression).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied."""
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_cadence_columns_present(db_engine: Engine) -> None:
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("goals")}
    assert "cadence_kind" in cols, "migration didn't add cadence_kind"
    assert "cadence_expr" in cols
    assert "cadence_tz" in cols
    assert not cols["cadence_kind"]["nullable"], "cadence_kind must be NOT NULL"
    assert cols["cadence_expr"]["nullable"]
    assert cols["cadence_tz"]["nullable"]


def test_cadence_kind_defaults_immediate(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO goals (id, title, status, priority, created_at, updated_at) "
                "VALUES (:id, 'x', 'open', 3, :ts, :ts)"
            ),
            {"id": "goal_cad_default", "ts": 1700000000},
        )
        conn.commit()
        row = conn.execute(
            sa.text(
                "SELECT cadence_kind, cadence_expr, cadence_tz "
                "FROM goals WHERE id = :id"
            ),
            {"id": "goal_cad_default"},
        ).one()
        assert row.cadence_kind == "immediate"
        assert row.cadence_expr is None
        assert row.cadence_tz is None
