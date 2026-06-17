"""add omnigent-native agent memory tables (FU1, BDP-2147)

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-17 00:00:00.000000

Creates the FU1 compartmented, weighted, decaying memory plane (ADR-0132):

- ``memory_compartments``: named, directly-queryable/appendable buckets
  (scope agent/team/topic — tenant deferred; ``half_life_seconds`` +
  ``read_floor`` / ``archive_floor`` drive decay/eviction).
- ``memories``: durable weighted memories with a ``search_text`` (FTS) column
  and an ``embedding`` column.

Dialect-aware: on PostgreSQL the ``vector`` extension is created, the
``embedding`` column is altered to ``vector(384)``, and an ivfflat + a
``tsvector`` GIN index are added (created here, before any growth driver
inserts rows, per ADR-0132). On SQLite the ``embedding`` column stays ``Text``
(lexical-only recall via the ``memories_fts`` FTS5 table the store creates).
All SQLite-incompatible DDL is guarded behind ``op.execute`` on the PG path,
and the regular tables are created with inline constraints inside
``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n1a2b3c4d5e6"
down_revision: str | None = "m1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5 (fastembed), in-pod, ADR-0132


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_compartments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("half_life_seconds", sa.Integer(), nullable=False),
        sa.Column("read_floor", sa.Float(), nullable=False, server_default="0.1"),
        sa.Column("archive_floor", sa.Float(), nullable=False, server_default="0.05"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "scope", "owner", "name", name="uq_memory_compartments_scope_owner_name"
        ),
        sa.CheckConstraint(
            "scope in ('agent', 'team', 'topic')", name="ck_memory_compartments_scope"
        ),
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("compartment_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_accessed_at", sa.Integer(), nullable=False),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_conversation_id", sa.String(length=64), nullable=True),
        sa.Column("source_compaction_id", sa.String(length=64), nullable=True),
        sa.Column("salience", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_model_version", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["compartment_id"], ["memory_compartments.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_memories_compartment_archived_weight",
        "memories",
        ["compartment_id", "archived", "weight"],
    )
    op.create_index(
        "ix_memories_compartment_archived_created",
        "memories",
        ["compartment_id", "archived", "created_at"],
    )
    op.create_index(
        "ix_memories_source_conversation_id", "memories", ["source_conversation_id"]
    )

    if is_pg:
        # Semantic recall path: real pgvector column + ivfflat cosine index,
        # plus a tsvector GIN index for lexical fallback — created up front,
        # before any growth driver inserts rows (ADR-0132).
        op.execute(
            "ALTER TABLE memories ALTER COLUMN embedding TYPE vector("
            f"{_EMBEDDING_DIM}) USING NULLIF(embedding, '')::vector"
        )
        op.execute(
            "CREATE INDEX ix_memories_embedding ON memories "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        op.execute(
            "CREATE INDEX ix_memories_search_text_tsv ON memories "
            "USING gin (to_tsvector('english', coalesce(search_text, '')))"
        )


def downgrade() -> None:
    op.drop_table("memories")
    op.drop_table("memory_compartments")
