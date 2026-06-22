"""add external_key to conversations (BDP-2390)

Revision ID: z4a2b3c4d5e6
Revises: z3a2b3c4d5e6
Create Date: 2026-06-22 01:00:00.000000

Adds the external correlation key for the bind-or-resume / idempotency seam
(ADR-0149). An external consumer (Office today) passes a stable `external_key`
on `POST /v1/sessions`; the server returns the existing session for a repeat
key instead of creating a duplicate (EIP Idempotent Receiver + Correlation
Identifier). Nullable `sa.String(128)` — NULL = no correlation (today's
behavior). A partial unique index `WHERE external_key IS NOT NULL` is the
single-writer guard that makes concurrent create-or-return race-safe (ADR-0009);
NULL rows are exempt so ordinary sessions are unaffected. SQLite-safe via batch
mode, mirroring `z3a2b3c4d5e6_add_tenant_id_to_conversations`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z4a2b3c4d5e6"
down_revision: str | None = "z3a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("external_key", sa.String(length=128), nullable=True))
    op.create_index(
        "uq_conversations_external_key",
        "conversations",
        ["external_key"],
        unique=True,
        sqlite_where=sa.text("external_key IS NOT NULL"),
        postgresql_where=sa.text("external_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_external_key", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("external_key")
