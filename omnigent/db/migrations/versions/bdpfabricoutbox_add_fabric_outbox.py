"""add fabric_outbox table

Revision ID: bdpfabricoutbox
Revises: bdpagentcategory
Create Date: 2026-06-26

Durable SQL-to-NATS outbox for the fabric cutover. SQL remains the source of
truth for schedule claims; this table records the canonical fabric envelope
before the NATS publisher emits it with ``Nats-Msg-Id = idempotency_key``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdpfabricoutbox"
down_revision: str | None = "bdpagentcategory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fabric_outbox",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("payload_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending', 'failed', 'published', 'dead_lettered')",
            name="ck_fabric_outbox_status",
        ),
    )
    op.create_index(
        "ix_fabric_outbox_status_next_attempt",
        "fabric_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index("ix_fabric_outbox_created", "fabric_outbox", ["created_at"])


def downgrade() -> None:
    op.drop_table("fabric_outbox")
