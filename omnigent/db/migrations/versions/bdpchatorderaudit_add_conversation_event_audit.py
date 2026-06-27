"""add conversation event audit table

Revision ID: bdpchatorderaudit
Revises: bdpgoaleconomics
Create Date: 2026-06-26

Records raw chat producer events next to the canonical conversation item log.
The canonical transcript stays in ``conversation_items``; this table lets the
sequencer prove every accepted raw event was persisted, buffered, released, or
ignored with an explicit reason.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdpchatorderaudit"
down_revision: str | None = "bdpgoaleconomics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_event_audit",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=64),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("provider_event_id", sa.String(length=256), nullable=True),
        sa.Column("response_id", sa.String(length=64), nullable=True),
        sa.Column("call_id", sa.String(length=128), nullable=True),
        sa.Column("message_id", sa.String(length=256), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("canonical_payload", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "conversation_item_id",
            sa.String(length=64),
            sa.ForeignKey("conversation_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "decision in ('received', 'persisted', 'buffered', 'released', "
            "'orphan_flushed', 'ignored')",
            name="ck_conversation_event_audit_decision",
        ),
    )
    op.create_index(
        "ix_conversation_event_audit_conversation_position",
        "conversation_event_audit",
        ["conversation_id", "position"],
        unique=True,
    )
    op.create_index(
        "ix_conversation_event_audit_conversation_created",
        "conversation_event_audit",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_conversation_event_audit_decision",
        "conversation_event_audit",
        ["decision"],
    )
    op.create_index(
        "ix_conversation_event_audit_call_id",
        "conversation_event_audit",
        ["conversation_id", "call_id"],
    )


def downgrade() -> None:
    op.drop_table("conversation_event_audit")
