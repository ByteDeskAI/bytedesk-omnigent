"""add goal scope, readiness, and dependencies

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-06-25 00:00:00.000000

Goals can now be framed for the organization, a department, or an individual
agent. Dependent goals keep their unblock conditions in a separate soft-reference
table so Omnigent can publish useful goal deltas without coupling to Platform
entities.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "target_kind",
                sa.String(length=16),
                nullable=False,
                server_default="organization",
            )
        )
        batch_op.add_column(
            sa.Column(
                "target_id",
                sa.String(length=128),
                nullable=False,
                server_default="omnigent",
            )
        )
        batch_op.add_column(sa.Column("target_label", sa.String(length=256), nullable=True))
        batch_op.add_column(
            sa.Column(
                "readiness_kind",
                sa.String(length=16),
                nullable=False,
                server_default="immediate",
            )
        )
        batch_op.add_column(
            sa.Column(
                "activation_state",
                sa.String(length=16),
                nullable=False,
                server_default="ready",
            )
        )

    op.create_index(
        "ix_goals_target_status",
        "goals",
        ["target_kind", "target_id", "status"],
    )
    op.create_index(
        "ix_goals_activation_status",
        "goals",
        ["activation_state", "status"],
    )

    op.create_table(
        "goal_dependencies",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("goal_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref", sa.String(length=256), nullable=True),
        sa.Column("label", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind in ('manual', 'goal', 'system_state')",
            name="ck_goal_dependencies_kind",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'satisfied', 'waived')",
            name="ck_goal_dependencies_status",
        ),
    )
    op.create_index("ix_goal_dependencies_goal", "goal_dependencies", ["goal_id"])
    op.create_index("ix_goal_dependencies_status", "goal_dependencies", ["status"])
    op.create_index(
        "ix_goal_dependencies_goal_status",
        "goal_dependencies",
        ["goal_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_goal_dependencies_goal_status", table_name="goal_dependencies")
    op.drop_index("ix_goal_dependencies_status", table_name="goal_dependencies")
    op.drop_index("ix_goal_dependencies_goal", table_name="goal_dependencies")
    op.drop_table("goal_dependencies")

    op.drop_index("ix_goals_activation_status", table_name="goals")
    op.drop_index("ix_goals_target_status", table_name="goals")
    with op.batch_alter_table("goals") as batch_op:
        batch_op.drop_column("activation_state")
        batch_op.drop_column("readiness_kind")
        batch_op.drop_column("target_label")
        batch_op.drop_column("target_id")
        batch_op.drop_column("target_kind")
