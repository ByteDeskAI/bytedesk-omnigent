"""make conversations.agent_id an external AgentStore reference

Revision ID: bdpagentstorenats
Revises: bdpchatordermerge
Create Date: 2026-06-27 00:00:00.000000

Drops the SQL FK from ``conversations.agent_id`` to ``agents.id`` so the active
AgentStore can be NATS-backed while the legacy ``agents`` table remains for
temporary verification. The column is preserved unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "bdpagentstorenats"
down_revision: str | Sequence[str] | None = "bdpchatordermerge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint("fk_conversations_agent_id", type_="foreignkey")


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.create_foreign_key(
            "fk_conversations_agent_id",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="CASCADE",
        )
