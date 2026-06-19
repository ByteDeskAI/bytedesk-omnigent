"""add capabilities to agents (BDP-2334)

Revision ID: z2a2b3c4d5e6
Revises: z1a2b3c4d5e6
Create Date: 2026-06-19 00:00:00.000000

Adds the first-class capability surface to the ``agents`` table: a JSON-encoded
list of capability slugs an agent declares (consumed by the assignment resolver,
ADR-0142). Stored as ``sa.Text()`` (JSON-in-Text), never native JSONB, so the
column is dual-DB safe (SQLite + Postgres) — mirrors ``hosts.configured_harnesses``
and ``tasks.payload``. NULL = no capabilities declared / not yet materialized.
Written via ``AgentStore.set_capabilities`` (mirrors the ``sot_tier`` setter).
SQLite-safe via batch mode.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z2a2b3c4d5e6"
down_revision: str | None = "z1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("capabilities", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("capabilities")
