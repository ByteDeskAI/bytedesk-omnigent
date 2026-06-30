"""add Omnigent connector framework tables

Revision ID: bdp2607connectors
Revises: bdp2606goaltax
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2607connectors"
down_revision: str | Sequence[str] | None = "bdp2606goaltax"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_connections",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("auth_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="connected"),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("secret_ref", sa.String(length=256), nullable=True),
        sa.Column("last_health_status", sa.String(length=32), nullable=True),
        sa.Column("last_health_at", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('connected', 'needs_reauth', 'disabled', 'error')",
            name="ck_connector_connections_status",
        ),
        sa.CheckConstraint(
            "auth_type in ('oauth_3lo', 'google_domain_wide_delegation')",
            name="ck_connector_connections_auth_type",
        ),
    )
    op.create_index(
        "ix_connector_connections_provider_status",
        "connector_connections",
        ["provider", "status"],
    )

    op.create_table(
        "connector_services",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("service_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint("connection_id", "service_key", name="uq_connector_services_key"),
        sa.CheckConstraint(
            "status in ('ready', 'disabled', 'error')",
            name="ck_connector_services_status",
        ),
    )
    op.create_index(
        "ix_connector_services_connection_enabled",
        "connector_services",
        ["connection_id", "enabled"],
    )

    op.create_table(
        "connector_agent_grants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("service_key", sa.String(length=64), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "connection_id",
            "agent_id",
            "service_key",
            "tool_key",
            name="uq_connector_agent_grants_tool",
        ),
        sa.CheckConstraint(
            "status in ('active', 'disabled', 'error')",
            name="ck_connector_agent_grants_status",
        ),
    )
    op.create_index(
        "ix_connector_agent_grants_agent_enabled",
        "connector_agent_grants",
        ["agent_id", "enabled"],
    )
    op.create_index(
        "ix_connector_agent_grants_connection",
        "connector_agent_grants",
        ["connection_id"],
    )

    op.create_table(
        "connector_oauth_states",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("state_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("requested_scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("redirect_uri", sa.String(length=512), nullable=False),
        sa.Column("code_verifier", sa.String(length=256), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_connector_oauth_states_provider_expires",
        "connector_oauth_states",
        ["provider", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_connector_oauth_states_provider_expires", table_name="connector_oauth_states")
    op.drop_table("connector_oauth_states")
    op.drop_index("ix_connector_agent_grants_connection", table_name="connector_agent_grants")
    op.drop_index("ix_connector_agent_grants_agent_enabled", table_name="connector_agent_grants")
    op.drop_table("connector_agent_grants")
    op.drop_index("ix_connector_services_connection_enabled", table_name="connector_services")
    op.drop_table("connector_services")
    op.drop_index("ix_connector_connections_provider_status", table_name="connector_connections")
    op.drop_table("connector_connections")
