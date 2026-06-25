"""add addressable memory key column + partial-unique live-slot index (BDP-2457)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-06-25 00:00:00.000000

Addressable (keyed) memory (the ADR-0132 addressable amendment) promotes the
memory "slot key" from the prototype encoding (``metadata = {"key": ...}``) to a
first-class ``memories.key`` column, so:

  * the keyed read/write/forget path filters by an indexed column instead of
    decoding JSON, and
  * a **partial unique index** ``(compartment_id, key) WHERE key IS NOT NULL AND
    NOT archived`` lets the DB enforce a single LIVE slot per key (the route no
    longer needs an in-process uniqueness check), while ambient (key NULL) and
    archived rows stay unconstrained.

The migration also **backfills** any prototype rows that stored the key inside
``metadata`` into the new column, so a DB written by the additive prototype
upgrades cleanly. It is idempotent — adding the column / index only when absent —
and dialect-portable (SQLite + PostgreSQL both honor the partial predicate).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARTIAL_WHERE = {
    "sqlite": "key IS NOT NULL AND archived = 0",
    "postgresql": "key IS NOT NULL AND archived = false",
}


def _has_column(bind: sa.Connection, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_index(bind: sa.Connection, table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) Add the nullable key column (idempotent).
    if not _has_column(bind, "memories", "key"):
        op.add_column("memories", sa.Column("key", sa.String(length=128), nullable=True))

    # 2) Backfill prototype metadata-encoded keys into the column. Only touches
    #    rows that have a JSON ``metadata.key`` and no column key yet.
    rows = bind.execute(
        sa.text(
            "SELECT id, metadata FROM memories "
            "WHERE metadata IS NOT NULL AND key IS NULL"
        )
    ).fetchall()
    for row in rows:
        try:
            parsed = json.loads(row.metadata)
        except (ValueError, TypeError):
            continue
        key = parsed.get("key") if isinstance(parsed, dict) else None
        if isinstance(key, str) and key:
            bind.execute(
                sa.text("UPDATE memories SET key = :k WHERE id = :id"),
                {"k": key, "id": row.id},
            )

    # 3) Partial unique index — one LIVE slot per (compartment, key) (idempotent).
    where = _PARTIAL_WHERE.get(dialect)
    if where is not None and not _has_index(bind, "memories", "uq_memories_compartment_key_live"):
        op.create_index(
            "uq_memories_compartment_key_live",
            "memories",
            ["compartment_id", "key"],
            unique=True,
            **{f"{dialect}_where": sa.text(where)},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "memories", "uq_memories_compartment_key_live"):
        op.drop_index("uq_memories_compartment_key_live", table_name="memories")
    if _has_column(bind, "memories", "key"):
        # SQLite cannot DROP COLUMN via a raw ALTER (pre-3.35); batch-recreate so
        # the downgrade is SQLite-safe (matches the migration-guard contract).
        with op.batch_alter_table("memories") as batch_op:
            batch_op.drop_column("key")
