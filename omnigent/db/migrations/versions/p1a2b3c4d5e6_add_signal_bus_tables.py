"""add durable signal/await bus tables (BDP-2248, ADR-0142)

Revision ID: p1a2b3c4d5e6
Revises: o1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

Creates the durable signal/await bus plane (ADR-0142, aligned ADR-0009):

- ``pending_waits``: the await registry. ``signal_id`` (the raw
  ``{runId}:{nodeId}`` colon form) is the PK *and* the idempotency key — a
  second deliver of the same id resolves to AlreadyResolved via the guarded
  conditional UPDATE.
- ``agent_messages``: durable inter-session inbox + Dead Letter Channel,
  replacing the ephemeral in-process queue so a wake survives a restart.

Dialect-neutral DDL (no pgvector/tsvector) — inline constraints inside
``op.create_table`` (SQLite-safe), so it succeeds on PostgreSQL and SQLite
unconditionally. ``session_id`` is a plain column (no FK) so the bus is
decoupled; orphans are reaper-swept.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p1a2b3c4d5e6"
down_revision: str | None = "o1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_waits",
        sa.Column("signal_id", sa.String(length=128), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending', 'resolved', 'expired')",
            name="ck_pending_waits_status",
        ),
    )
    op.create_index(
        "ix_pending_waits_kind_target", "pending_waits", ["kind", "target"]
    )
    op.create_index(
        "ix_pending_waits_session_status", "pending_waits", ["session_id", "status"]
    )
    op.create_index(
        "ix_pending_waits_status_expires", "pending_waits", ["status", "expires_at"]
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("signal_id", sa.String(length=128), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "dead_lettered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_session_delivered_seq",
        "agent_messages",
        ["session_id", "delivered_at", "seq"],
    )
    op.create_index(
        "ix_agent_messages_dead_lettered", "agent_messages", ["dead_lettered"]
    )


def downgrade() -> None:
    op.drop_table("agent_messages")
    op.drop_table("pending_waits")
