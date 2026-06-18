"""add native cron trigger table (BDP-2250, ADR-0142)

Revision ID: q1a2b3c4d5e6
Revises: p1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

Creates ``cron_triggers`` — the durable schedule registry for the native cron
scheduler (the server ``_lifespan`` loop). A due trigger is claimed via a
guarded UPDATE on ``(id, next_fire_at)`` (exactly-once per fire instant,
ADR-0009) and dispatched by opening/resuming the agent's session. Replaces the
no-op ``cadence:`` bundle param and the stubbed ``sys_timer_set``.

Dialect-neutral DDL — inline constraints inside ``op.create_table``
(SQLite-safe). ``agent_id`` is a plain column (no FK) so the scheduler is
decoupled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q1a2b3c4d5e6"
down_revision: str | None = "p1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cron_triggers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("schedule_kind", sa.String(length=16), nullable=False),
        sa.Column("schedule_expr", sa.String(length=128), nullable=False),
        sa.Column("next_fire_at", sa.Integer(), nullable=False),
        sa.Column("last_fired_at", sa.Integer(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint("agent_id", "key", name="uq_cron_triggers_agent_key"),
        sa.CheckConstraint(
            "schedule_kind in ('interval', 'cron', 'once')",
            name="ck_cron_triggers_schedule_kind",
        ),
    )
    op.create_index(
        "ix_cron_triggers_enabled_next_fire",
        "cron_triggers",
        ["enabled", "next_fire_at"],
    )


def downgrade() -> None:
    op.drop_table("cron_triggers")
