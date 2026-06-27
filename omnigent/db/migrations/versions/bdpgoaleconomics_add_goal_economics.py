"""add goal economics columns + treasury tables (BDP-2585, Phase 3)

Revision ID: bdpgoaleconomics
Revises: bdpgoalcadence
Create Date: 2026-06-26 12:00:00.000000

Turns the goal into an economic unit: it carries expected/realized value,
confidence, risk tier, an explicit org/department/agent tier and a parent goal,
plus an optional success-condition AST ref. The Treasury gets three tables —
``goal_budgets`` (caps + spend, the circuit-breaker state), ``goal_outcomes``
(the realized-value ledger, written only by ``book_outcome``) and
``goal_decisions`` (every fund/skip decision with its ROI rationale — replay).

All economics columns default so an existing goal is unchanged. Dialect-neutral
DDL — ``batch_alter_table`` for SQLite ALTER, inline constraints in create_table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdpgoaleconomics"
down_revision: str | None = "bdpgoalcadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.add_column(
            sa.Column("tier", sa.String(length=16), nullable=False, server_default="org")
        )
        batch_op.add_column(sa.Column("parent_goal_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("expected_value_cents", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("realized_value_cents", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5")
        )
        batch_op.add_column(
            sa.Column("risk_tier", sa.String(length=16), nullable=False, server_default="low")
        )
        batch_op.add_column(sa.Column("success_condition", sa.Text(), nullable=True))
    op.create_index("ix_goals_parent", "goals", ["parent_goal_id"])

    op.create_table(
        "goal_budgets",
        sa.Column("tier", sa.String(length=16), primary_key=True),
        sa.Column("target_id", sa.String(length=128), primary_key=True),
        sa.Column("cap_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spent_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cap_tokens", sa.Integer(), nullable=True),
        sa.Column("spent_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_spawns", sa.Integer(), nullable=True),
        sa.Column("spawns_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("anomaly_threshold_cents", sa.Integer(), nullable=True),
        sa.Column("circuit_open", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )

    op.create_table(
        "goal_outcomes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("goal_id", sa.String(length=64), nullable=False),
        sa.Column("booked_at", sa.Integer(), nullable=False),
        sa.Column("realized_value_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
    )
    op.create_index("ix_goal_outcomes_goal", "goal_outcomes", ["goal_id"])

    op.create_table(
        "goal_decisions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tick_id", sa.String(length=64), nullable=False),
        sa.Column("goal_id", sa.String(length=64), nullable=False),
        sa.Column("roi_at_decision", sa.Float(), nullable=False, server_default="0"),
        sa.Column("budget_before", sa.Integer(), nullable=True),
        sa.Column("budget_after", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("spawned_session_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_goal_decisions_goal", "goal_decisions", ["goal_id"])
    op.create_index("ix_goal_decisions_tick", "goal_decisions", ["tick_id"])


def downgrade() -> None:
    op.drop_table("goal_decisions")
    op.drop_table("goal_outcomes")
    op.drop_table("goal_budgets")
    op.drop_index("ix_goals_parent", table_name="goals")
    with op.batch_alter_table("goals") as batch_op:
        batch_op.drop_column("success_condition")
        batch_op.drop_column("risk_tier")
        batch_op.drop_column("confidence")
        batch_op.drop_column("realized_value_cents")
        batch_op.drop_column("expected_value_cents")
        batch_op.drop_column("parent_goal_id")
        batch_op.drop_column("tier")
