"""pgvector capability probes for the FU1 memory plane (BDP-2147, ADR-0132).

Semantic recall needs the PostgreSQL ``vector`` extension. Not every Postgres
ships it — local/dev images and some managed Postgres instances don't — so both
the schema migration and the runtime embedder gate on these probes instead of
assuming ``dialect == 'postgresql'`` implies pgvector. When the extension is
absent the memory plane degrades to lexical recall (the ``tsvector`` GIN index),
which works on any Postgres; semantic recall layers on additively when pgvector
is present. These two probes are the single capability seam shared by the
migration and the runtime so the two never disagree.
"""

from __future__ import annotations

import sqlalchemy as sa


def pgvector_available(conn: sa.Connection) -> bool:
    """Whether the ``vector`` extension is *installable* on this connection.

    True when ``pg_available_extensions`` lists ``vector`` (the control file is
    present on the server). The migration uses this to decide whether to
    ``CREATE EXTENSION`` and build the ``vector(384)`` column + ivfflat index.
    Non-PostgreSQL connections always return ``False``.
    """
    if conn.dialect.name != "postgresql":
        return False
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).first()
    return row is not None


def pgvector_installed(conn: sa.Connection) -> bool:
    """Whether the ``vector`` extension is *installed* on this database.

    True when ``pg_extension`` contains ``vector`` — i.e. the migration ran on a
    Postgres that had it available and created the ``vector`` column. The runtime
    uses this to decide whether to attach the embedder (semantic recall) or stay
    lexical; attaching it on a database whose ``embedding`` column is still
    ``TEXT`` would make recall's ``embedding <=> CAST(... AS vector)`` fail.
    Non-PostgreSQL connections always return ``False``.
    """
    if conn.dialect.name != "postgresql":
        return False
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).first()
    return row is not None
