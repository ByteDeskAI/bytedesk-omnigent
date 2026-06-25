"""retroactively build memory.embedding pgvector column when available (BDP-2457)

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e6f7
Create Date: 2026-06-25 00:00:00.000000

The original add_memory_tables migration (n1a2b3c4d5e6) is capability-aware: on a
Postgres WITHOUT pgvector it leaves ``memories.embedding`` as ``Text`` and recall
degrades to lexical. The omnigent Postgres shipped as plain ``postgres:16-alpine``
(no pgvector), so semantic recall never turned on.

BDP-2457 switches the image to ``pgvector/pgvector:pg16``. This migration builds
the semantic substrate the original one skipped — ``CREATE EXTENSION vector``,
``embedding -> vector(384)``, and the ivfflat cosine index — but ONLY when
pgvector is now available and the column is still ``Text``. It is a no-op when:

  * the dialect is not PostgreSQL (SQLite stays lexical / FTS5),
  * pgvector is still unavailable (stays lexical / tsvector GIN), or
  * the column is already ``vector`` (a fresh DB whose add_memory_tables already
    built it on a pgvector image),

so it is idempotent and safe on every existing and new database. Dimension and
index match add_memory_tables exactly (the runtime ``pgvector_installed`` gate
and the embedder both assume ``vector(384)``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "a1b2c3d4e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5 (fastembed) — must match add_memory_tables


def _embedding_udt(bind: sa.Connection) -> str | None:
    """``udt_name`` of ``memories.embedding`` (``text`` / ``vector``), or None if absent."""
    row = bind.execute(
        sa.text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name = 'embedding'"
        )
    ).first()
    return row.udt_name.lower() if row and row.udt_name else None


def upgrade() -> None:
    from omnigent.stores.memory_store.pgvector import pgvector_available

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite: lexical-only (FTS5) — nothing to upgrade.
    if not pgvector_available(bind):
        return  # No pgvector control file: stay lexical (tsvector GIN index).
    if _embedding_udt(bind) != "text":
        return  # Already vector (fresh pgvector DB) or table absent: idempotent no-op.

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE memories ALTER COLUMN embedding TYPE "
        f"vector({_EMBEDDING_DIM}) USING NULLIF(embedding, '')::vector"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_embedding ON memories "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _embedding_udt(bind) != "vector":
        return  # Not upgraded by this migration: nothing to revert.
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding")
    op.execute(
        "ALTER TABLE memories ALTER COLUMN embedding TYPE text USING embedding::text"
    )
