"""add tasks table — a goal with assignment + execution binding (BDP-2333, ADR-0142)

Revision ID: z1a2b3c4d5e6
Revises: y1a2b3c4d5e6
Create Date: 2026-06-19 00:00:00.000000

A first-class task: the goal substrate (BDP-2271 C3) plus an explicit execution
binding — ``assignee_agent_id`` (who runs it) distinct from ``owner_agent_id`` (who
is accountable) — and a ``required_capability`` that gates which agent may be
assigned. ``claim_task`` is a guarded UPDATE on ``(id, status='open')`` so exactly
one agent claims a task (ADR-0009). Soft FKs (plain columns) on the agent ids;
``payload`` is JSON-in-Text (dual-DB SQLite + Postgres), never native JSONB.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z1a2b3c4d5e6"
down_revision: str | None = "y1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("owner_agent_id", sa.String(length=64), nullable=True),
        sa.Column("assignee_agent_id", sa.String(length=64), nullable=True),
        sa.Column("required_capability", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('open', 'assigned', 'in_progress', 'blocked', 'done')",
            name="ck_tasks_status",
        ),
    )
    op.create_index("ix_tasks_status_priority", "tasks", ["status", "priority"])
    op.create_index("ix_tasks_owner_status", "tasks", ["owner_agent_id", "status"])
    op.create_index("ix_tasks_assignee_status", "tasks", ["assignee_agent_id", "status"])


def downgrade() -> None:
    op.drop_table("tasks")
