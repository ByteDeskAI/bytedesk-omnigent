"""add goal_templates table (BDP-2588, Phase 6a)

Revision ID: bdp2588goaltmpl
Revises: bdpgoaleconomics
Create Date: 2026-06-26 14:00:00.000000

Reusable goal blueprints for the admin CRUD surface: a ``goal_templates`` row is
the JSON ``definition`` (cadence, conditions, budget, risk_tier, target framing,
default payload) plus a unique ``name``. Instantiating one creates a normal
``goals`` row from the definition merged with per-call overrides.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2588goaltmpl"
down_revision: str | None = "bdpgoaleconomics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goal_templates",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name", name="uq_goal_templates_name"),
    )


def downgrade() -> None:
    op.drop_table("goal_templates")
