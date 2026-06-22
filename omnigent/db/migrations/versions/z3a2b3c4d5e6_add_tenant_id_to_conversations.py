"""add tenant_id to conversations (BDP-2388)

Revision ID: z3a2b3c4d5e6
Revises: z2a2b3c4d5e6
Create Date: 2026-06-22 00:00:00.000000

Adds the optional tenant scope to the ``conversations`` table (ADR-0149,
the Identity/Tenancy pluggability seam). The tenant is resolved from the
request Principal at session-create time and persisted here so an external
consumer's sessions are durable + queryable by tenant. Stored as a plain
nullable ``sa.String(64)`` — NULL = today's single-org / local behavior, so
existing rows backfill to NULL with zero behavior change. Low-cardinality
and not yet a filter predicate (cross-tenant enforcement lands with the
Office consumer, BDP-2395), so no index. SQLite-safe via batch mode,
mirroring ``z2a2b3c4d5e6_add_capabilities_to_agents``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z3a2b3c4d5e6"
down_revision: str | None = "z2a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("tenant_id")
