"""add goal taxonomy and provider outcome correlations

Revision ID: bdp2606goaltax
Revises: bdpagentstorenats
Create Date: 2026-06-29

Adds the queryable taxonomy fields needed by the portfolio command center and a
provider-language-agnostic correlation table for outcome booking. Existing goals
default to financial organization work, preserving the prior economic behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2606goaltax"
down_revision: str | Sequence[str] | None = "bdpagentstorenats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.add_column(sa.Column("department_slug", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column(
                "outcome_kind",
                sa.String(length=32),
                nullable=False,
                server_default="financial",
            )
        )
        batch_op.create_check_constraint(
            "ck_goals_outcome_kind",
            "outcome_kind in ('financial', 'roadmap', 'capability', 'risk', 'operational')",
        )

    op.create_index("ix_goals_department_status", "goals", ["department_slug", "status"])
    op.create_index("ix_goals_outcome_kind_status", "goals", ["outcome_kind", "status"])

    op.create_table(
        "goal_correlations",
        sa.Column("source", sa.String(length=64), primary_key=True),
        sa.Column("subject_ref", sa.String(length=256), primary_key=True),
        sa.Column("goal_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_goal_correlations_goal", "goal_correlations", ["goal_id"])
    op.create_index("ix_goal_correlations_tenant", "goal_correlations", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_goal_correlations_tenant", table_name="goal_correlations")
    op.drop_index("ix_goal_correlations_goal", table_name="goal_correlations")
    op.drop_table("goal_correlations")
    op.drop_index("ix_goals_outcome_kind_status", table_name="goals")
    op.drop_index("ix_goals_department_status", table_name="goals")
    with op.batch_alter_table("goals") as batch_op:
        batch_op.drop_constraint("ck_goals_outcome_kind", type_="check")
        batch_op.drop_column("outcome_kind")
        batch_op.drop_column("department_slug")
