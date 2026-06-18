"""add tool_steps table (BDP-2252 α5, ADR-0142)

Revision ID: u1a2b3c4d5e6
Revises: t1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The durable deterministic tool-step substrate: a tool-step is claimed once
(idempotent by ``(session_id, step_key)``), executed, and recorded completed
(with its cached result for deterministic re-entry) or failed. Retry-over-session
returns a sub-cap failure to ``pending``; resume-on-restart reclaims a ``running``
step past its ``deadline_at``.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u1a2b3c4d5e6"
down_revision: str | None = "t1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_steps",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("step_key", sa.String(length=256), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("deadline_at", sa.Integer(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "session_id", "step_key", name="uq_tool_steps_session_step"
        ),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'completed', 'failed')",
            name="ck_tool_steps_status",
        ),
    )
    op.create_index(
        "ix_tool_steps_status_deadline", "tool_steps", ["status", "deadline_at"]
    )


def downgrade() -> None:
    op.drop_table("tool_steps")
