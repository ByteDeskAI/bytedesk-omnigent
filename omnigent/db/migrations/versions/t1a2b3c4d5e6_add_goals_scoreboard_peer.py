"""add goals + ops scoreboard + peer-message tables (BDP-2271 C3 / BDP-2270 C2, ADR-0142)

Revision ID: t1a2b3c4d5e6
Revises: s1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The "why-act" substrate + the lateral social fabric:
- ``goals``: a durable backlog an agent pulls + owns (claim is a guarded UPDATE).
- ``scoreboard_entries``: per-(agent, metric, window) ops metrics.
- ``peer_messages``: durable lateral peer messages.

Dialect-neutral DDL — inline constraints inside ``op.create_table`` (SQLite-safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t1a2b3c4d5e6"
down_revision: str | None = "s1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("owner_agent_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('open', 'assigned', 'in_progress', 'blocked', 'done')",
            name="ck_goals_status",
        ),
    )
    op.create_index("ix_goals_status_priority", "goals", ["status", "priority"])
    op.create_index("ix_goals_owner_status", "goals", ["owner_agent_id", "status"])

    op.create_table(
        "scoreboard_entries",
        sa.Column("agent_id", sa.String(length=64), primary_key=True),
        sa.Column("metric", sa.String(length=64), primary_key=True),
        sa.Column("window", sa.String(length=32), primary_key=True, server_default="all"),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_scoreboard_metric_window_value",
        "scoreboard_entries",
        ["metric", "window", "value"],
    )

    op.create_table(
        "peer_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_agent", sa.String(length=64), nullable=False),
        sa.Column("to_agent", sa.String(length=64), nullable=True),
        sa.Column("topic", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="dm"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind in ('dm', 'broadcast', 'escalation')", name="ck_peer_messages_kind"
        ),
    )
    op.create_index(
        "ix_peer_messages_to_read_seq", "peer_messages", ["to_agent", "read_at", "seq"]
    )
    op.create_index("ix_peer_messages_topic_seq", "peer_messages", ["topic", "seq"])


def downgrade() -> None:
    op.drop_table("peer_messages")
    op.drop_table("scoreboard_entries")
    op.drop_table("goals")
