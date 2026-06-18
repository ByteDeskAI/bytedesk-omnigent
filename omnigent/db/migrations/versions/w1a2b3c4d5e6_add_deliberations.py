"""add deliberations + deliberation_positions tables (BDP-2273 C6, ADR-0142)

Revision ID: w1a2b3c4d5e6
Revises: v1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The decision organ: a durable proposal→debate→decision so "what did we decide
about X?" is queryable. A deliberation opens with a proposal, accumulates
positions (for/against/amend) across rounds, and closes with a recorded decision.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w1a2b3c4d5e6"
down_revision: str | None = "v1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliberations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("topic", sa.String(length=256), nullable=False),
        sa.Column("proposal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("opened_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('open', 'decided', 'closed')", name="ck_deliberations_status"
        ),
    )
    op.create_index(
        "ix_deliberations_topic_status", "deliberations", ["topic", "status"]
    )

    op.create_table(
        "deliberation_positions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("deliberation_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "stance in ('for', 'against', 'amend')",
            name="ck_deliberation_positions_stance",
        ),
    )
    op.create_index(
        "ix_deliberation_positions_delib_round",
        "deliberation_positions",
        ["deliberation_id", "round"],
    )


def downgrade() -> None:
    op.drop_table("deliberation_positions")
    op.drop_table("deliberations")
