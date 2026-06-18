"""add inbound-webhook binding table (BDP-2249, ADR-0142)

Revision ID: s1a2b3c4d5e6
Revises: r1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

Creates ``webhook_bindings`` — maps an inbound external event ``(source,
match_key)`` to a durable ``signal_id``. The signed ingress route verifies the
HMAC, resolves the binding, and delivers to the signal bus (BDP-2248). Unmatched
events 404 (BDP-1419).

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s1a2b3c4d5e6"
down_revision: str | None = "r1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_bindings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("match_key", sa.String(length=256), nullable=False, server_default="*"),
        sa.Column("signal_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint("source", "match_key", name="uq_webhook_bindings_source_match"),
    )
    op.create_index(
        "ix_webhook_bindings_source_enabled",
        "webhook_bindings",
        ["source", "enabled"],
    )


def downgrade() -> None:
    op.drop_table("webhook_bindings")
