"""add Work Force builtin tool permissions

Revision ID: bdp2609wftools
Revises: bdp2608workforce
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2609wftools"
down_revision: str | Sequence[str] | None = "bdp2608workforce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_tool_assignments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("tool_key", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "scope_kind",
            "scope_id",
            "tool_key",
            name="uq_workforce_tool_assignment_tool",
        ),
        sa.CheckConstraint(
            "scope_kind in ('organization', 'department')",
            name="ck_workforce_tool_assignments_scope_kind",
        ),
    )
    op.create_index(
        "ix_workforce_tool_assignments_scope",
        "workforce_tool_assignments",
        ["scope_kind", "scope_id", "enabled"],
    )

    with op.batch_alter_table("workforce_agent_overrides") as batch_op:
        batch_op.drop_constraint("ck_workforce_agent_overrides_item_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_workforce_agent_overrides_item_kind",
            "item_kind in ('connector', 'skill', 'tool')",
        )

    with op.batch_alter_table("workforce_agent_materializations") as batch_op:
        batch_op.drop_constraint("ck_workforce_agent_materializations_item_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_workforce_agent_materializations_item_kind",
            "item_kind in ('connector', 'skill', 'tool')",
        )


def downgrade() -> None:
    with op.batch_alter_table("workforce_agent_materializations") as batch_op:
        batch_op.drop_constraint("ck_workforce_agent_materializations_item_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_workforce_agent_materializations_item_kind",
            "item_kind in ('connector', 'skill')",
        )

    with op.batch_alter_table("workforce_agent_overrides") as batch_op:
        batch_op.drop_constraint("ck_workforce_agent_overrides_item_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_workforce_agent_overrides_item_kind",
            "item_kind in ('connector', 'skill')",
        )

    op.drop_index(
        "ix_workforce_tool_assignments_scope",
        table_name="workforce_tool_assignments",
    )
    op.drop_table("workforce_tool_assignments")
