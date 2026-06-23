"""add version column to config stores for optimistic concurrency (BDP-2412)

Revision ID: z5a2b3c4d5e6
Revises: z4a2b3c4d5e6
Create Date: 2026-06-23 02:00:00.000000

Adds a monotonic integer ``version`` ETag to the five writable config stores
(policies, conversations, session_permissions, cron_triggers, webhook_bindings)
so the config control plane (ADR-0150) can guard writes with an ``If-Match``
compare-and-swap (BDP-2412), closing last-writer-wins clobbers. ``NOT NULL`` with
``server_default='1'`` backfills every pre-existing row to version 1 on a
populated table (no separate UPDATE), mirroring ``agents.version``
(43fb65b29464). SQLite-safe via ``batch_alter_table``, like the tenant_id /
external_key migrations. All five tables share this one alembic chain (the
bytedesk_omnigent ext tables are in-chain, not a separate environment), so a
single revision covers them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z5a2b3c4d5e6"
down_revision: str | None = "z4a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "policies",
    "conversations",
    "session_permissions",
    "cron_triggers",
    "webhook_bindings",
)


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("version", sa.Integer(), nullable=False, server_default="1")
            )


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("version")
