"""add Work Force inheritance tables

Revision ID: bdp2608workforce
Revises: bdp2607connectors
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2608workforce"
down_revision: str | Sequence[str] | None = "bdp2607connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workforce_instructions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint("scope_kind", "scope_id", name="uq_workforce_instructions_scope"),
        sa.CheckConstraint(
            "scope_kind in ('organization', 'department', 'agent')",
            name="ck_workforce_instructions_scope_kind",
        ),
    )
    op.create_index(
        "ix_workforce_instructions_scope",
        "workforce_instructions",
        ["scope_kind", "scope_id"],
    )

    op.create_table(
        "workforce_connector_assignments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("service_key", sa.String(length=64), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "scope_kind",
            "scope_id",
            "connection_id",
            "service_key",
            "tool_key",
            name="uq_workforce_connector_assignment_tool",
        ),
        sa.CheckConstraint(
            "scope_kind in ('organization', 'department')",
            name="ck_workforce_connector_assignments_scope_kind",
        ),
    )
    op.create_index(
        "ix_workforce_connector_assignments_scope",
        "workforce_connector_assignments",
        ["scope_kind", "scope_id", "enabled"],
    )
    op.create_index(
        "ix_workforce_connector_assignments_connection",
        "workforce_connector_assignments",
        ["connection_id", "service_key", "tool_key"],
    )

    op.create_table(
        "workforce_skill_assignments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "scope_kind",
            "scope_id",
            "skill_name",
            name="uq_workforce_skill_assignment_skill",
        ),
        sa.CheckConstraint(
            "scope_kind in ('organization', 'department')",
            name="ck_workforce_skill_assignments_scope_kind",
        ),
    )
    op.create_index(
        "ix_workforce_skill_assignments_scope",
        "workforce_skill_assignments",
        ["scope_kind", "scope_id", "enabled"],
    )

    op.create_table(
        "workforce_agent_overrides",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("item_kind", sa.String(length=32), nullable=False),
        sa.Column("item_key", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "agent_id",
            "item_kind",
            "item_key",
            name="uq_workforce_agent_override_item",
        ),
        sa.CheckConstraint(
            "item_kind in ('connector', 'skill')",
            name="ck_workforce_agent_overrides_item_kind",
        ),
    )
    op.create_index(
        "ix_workforce_agent_overrides_agent",
        "workforce_agent_overrides",
        ["agent_id", "item_kind"],
    )

    op.create_table(
        "workforce_agent_materializations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("item_kind", sa.String(length=32), nullable=False),
        sa.Column("item_key", sa.String(length=512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "agent_id",
            "item_kind",
            "item_key",
            name="uq_workforce_agent_materialization_item",
        ),
        sa.CheckConstraint(
            "item_kind in ('connector', 'skill')",
            name="ck_workforce_agent_materializations_item_kind",
        ),
    )
    op.create_index(
        "ix_workforce_agent_materializations_agent",
        "workforce_agent_materializations",
        ["agent_id", "item_kind", "active"],
    )

    op.create_table(
        "workforce_revisions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("workforce_revisions")
    op.drop_index(
        "ix_workforce_agent_materializations_agent",
        table_name="workforce_agent_materializations",
    )
    op.drop_table("workforce_agent_materializations")
    op.drop_index("ix_workforce_agent_overrides_agent", table_name="workforce_agent_overrides")
    op.drop_table("workforce_agent_overrides")
    op.drop_index(
        "ix_workforce_skill_assignments_scope",
        table_name="workforce_skill_assignments",
    )
    op.drop_table("workforce_skill_assignments")
    op.drop_index(
        "ix_workforce_connector_assignments_connection",
        table_name="workforce_connector_assignments",
    )
    op.drop_index(
        "ix_workforce_connector_assignments_scope",
        table_name="workforce_connector_assignments",
    )
    op.drop_table("workforce_connector_assignments")
    op.drop_index("ix_workforce_instructions_scope", table_name="workforce_instructions")
    op.drop_table("workforce_instructions")
