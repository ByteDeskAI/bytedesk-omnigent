"""add suppressions table (BDP-2278 F3, ADR-0142)

Revision ID: x1a2b3c4d5e6
Revises: w1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The outreach-compliance floor: a do-not-contact list keyed ``(channel, address)``
(opt-out / GDPR erasure / hard bounce / complaint). The outreach path checks it
before sending — an obligation an agent cannot talk its way past.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x1a2b3c4d5e6"
down_revision: str | None = "w1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppressions",
        sa.Column("channel", sa.String(length=16), primary_key=True),
        sa.Column("address", sa.String(length=320), primary_key=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason in ('unsubscribe', 'gdpr_erasure', 'bounce', 'complaint', 'manual')",
            name="ck_suppressions_reason",
        ),
    )


def downgrade() -> None:
    op.drop_table("suppressions")
