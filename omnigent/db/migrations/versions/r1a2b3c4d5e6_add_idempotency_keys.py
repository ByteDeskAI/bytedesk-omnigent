"""add durable idempotency-key table (BDP-2251, ADR-0142)

Revision ID: r1a2b3c4d5e6
Revises: q1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

Creates ``idempotency_keys`` — a generic at-most-once claim plane (ADR-0009/0077).
A consumer claims a ``(scope, key)`` before doing work; the composite PK is the
atomic guard (a duplicate insert hits the conflict). Replaces the per-consumer
``DbSupportTicketIdempotencyStore`` / ``WorkflowTriggerInboxEntry`` so the
event-trigger re-homes dedup against one durable plane.

Dialect-neutral DDL — inline composite PK + ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r1a2b3c4d5e6"
down_revision: str | None = "q1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.String(length=64), primary_key=True),
        sa.Column("key", sa.String(length=256), primary_key=True),
        sa.Column("claimed_at", sa.Integer(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column(
            "dead_lettered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_idempotency_keys_dead_lettered", "idempotency_keys", ["dead_lettered"]
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
