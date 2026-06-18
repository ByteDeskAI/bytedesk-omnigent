"""add business_outcomes table (BDP-2268 B7, ADR-0142)

Revision ID: v1a2b3c4d5e6
Revises: u1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The org's outcome ledger: an append-only record of attributed business outcomes
(won deal, resolved ticket, shipped feature). Recording an outcome rolls up into
the agent's cumulative ``scoreboard_entries`` metric, feeding find-specialist
ranking — the org learns who is good at what.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v1a2b3c4d5e6"
down_revision: str | None = "u1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "business_outcomes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False, server_default="1"),
        sa.Column("ref", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_business_outcomes_agent_metric",
        "business_outcomes",
        ["agent_id", "metric"],
    )
    op.create_index("ix_business_outcomes_kind", "business_outcomes", ["kind"])


def downgrade() -> None:
    op.drop_table("business_outcomes")
