"""add agentic inbox email event table (BDP-2455)

Revision ID: a1b2c3d4e6f7
Revises: z5a2b3c4d5e6
Create Date: 2026-06-24 12:00:00.000000

Creates ``agentic_inbox_events`` for signed Agentic Inbox ``email.received``
webhooks. The event id is the idempotency key; status/session columns make
dispatch observable and keep unknown-mailbox events as durable dead letters.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e6f7"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agentic_inbox_events",
        sa.Column("event_id", sa.String(length=256), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("mailbox_id", sa.String(length=320), nullable=False),
        sa.Column("email_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=512), nullable=True),
        sa.Column("sender", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("received_at", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("dispatched_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status in ('received', 'dispatched', 'dead_lettered', 'failed')",
            name="ck_agentic_inbox_events_status",
        ),
    )
    op.create_index(
        "ix_agentic_inbox_events_mailbox_status",
        "agentic_inbox_events",
        ["mailbox_id", "status"],
    )
    op.create_index(
        "ix_agentic_inbox_events_status_updated",
        "agentic_inbox_events",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_agentic_inbox_events_agent",
        "agentic_inbox_events",
        ["agent_id"],
    )


def downgrade() -> None:
    op.drop_table("agentic_inbox_events")
