"""Tests for the pgvector capability seam (BDP-2147).

The probes + the runtime embedder-selection gate must degrade to lexical recall
when the PostgreSQL ``vector`` extension is absent, so a Postgres without
pgvector (local/dev images, some managed PG) boots instead of crash-looping the
migration / failing recall. The real pgvector path is covered by the opt-in
Postgres integration suite; here we verify the dialect/probe logic with SQLite
and a stub connection.
"""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.stores.memory_store.pgvector import (
    pgvector_available,
    pgvector_installed,
)


class _StubResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _StubConn:
    """Minimal SQLAlchemy-Connection stand-in for the postgresql probe branch."""

    def __init__(self, *, dialect_name="postgresql", row=(1,)):
        self.dialect = type("D", (), {"name": dialect_name})
        self._row = row
        self.executed: list[str] = []

    def execute(self, clause):
        self.executed.append(str(clause))
        return _StubResult(self._row)


def test_probes_false_on_sqlite(tmp_path):
    """Non-PostgreSQL connections never claim pgvector — no SQL is issued."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.connect() as conn:
        assert pgvector_available(conn) is False
        assert pgvector_installed(conn) is False


def test_available_true_when_extension_listed():
    conn = _StubConn(row=(1,))
    assert pgvector_available(conn) is True
    assert "pg_available_extensions" in conn.executed[0]


def test_available_false_when_extension_absent():
    conn = _StubConn(row=None)
    assert pgvector_available(conn) is False


def test_installed_true_when_extension_present():
    conn = _StubConn(row=(1,))
    assert pgvector_installed(conn) is True
    assert "pg_extension" in conn.executed[0]


def test_installed_false_when_extension_missing():
    conn = _StubConn(row=None)
    assert pgvector_installed(conn) is False


def test_select_embedder_none_on_sqlite(tmp_path):
    """The runtime never loads the embedding model on SQLite (lexical-only)."""
    from omnigent.runtime import _select_memory_embedder

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    assert _select_memory_embedder(engine) is None


def test_select_embedder_none_on_pg_without_pgvector(monkeypatch, tmp_path):
    """On Postgres without the extension installed, recall stays lexical — the
    embedder is NOT attached (so recall never casts a TEXT column to vector)."""
    from omnigent import runtime

    monkeypatch.setattr(runtime, "_pgvector_installed", lambda conn: False)

    class _FakePGEngine:
        dialect = type("D", (), {"name": "postgresql"})

        def connect(self):
            class _Ctx:
                def __enter__(self_inner):
                    return object()

                def __exit__(self_inner, *a):
                    return False

            return _Ctx()

    assert runtime._select_memory_embedder(_FakePGEngine()) is None
