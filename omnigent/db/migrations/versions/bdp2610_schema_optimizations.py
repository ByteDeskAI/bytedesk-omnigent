"""schema index, FK, and search optimizations (audit follow-up)

Revision ID: bdp2610schemaopt
Revises: bdp2609wftools
Create Date: 2026-07-01

Adds hot-path indexes (session listing, runner/agent lookups, goals claimable
composite, hosts updated_at, memory sweep, connector OAuth expiry, conversation
item type/position), drops two redundant indexes, adds SQLite-safe foreign keys
for child tables that should cascade with parents, and creates a Postgres-only
GIN full-text index on ``conversation_items.search_text``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2610schemaopt"
down_revision: str | Sequence[str] | None = "bdp2609wftools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _delete_orphaned_child_rows() -> None:
    """Remove child rows that would block FK creation on upgraded databases."""
    op.execute(
        sa.text(
            "DELETE FROM comments "
            "WHERE conversation_id IS NULL "
            "OR conversation_id NOT IN (SELECT id FROM conversations)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM goal_dependencies "
            "WHERE goal_id NOT IN (SELECT id FROM goals)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM goal_outcomes WHERE goal_id NOT IN (SELECT id FROM goals)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM inbound_event_results "
            "WHERE idempotency_key NOT IN (SELECT idempotency_key FROM inbound_events)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM connector_services "
            "WHERE connection_id NOT IN (SELECT id FROM connector_connections)"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM connector_agent_grants "
            "WHERE connection_id NOT IN (SELECT id FROM connector_connections)"
        )
    )


def upgrade() -> None:
    op.create_index("ix_conversations_runner_id", "conversations", ["runner_id"])
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])
    op.create_index(
        "ix_conversations_active_sessions",
        "conversations",
        [sa.text("updated_at DESC"), sa.text("id DESC")],
        sqlite_where=sa.text(
            "kind = 'default' AND archived = 0 AND agent_id IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "kind = 'default' AND archived = false AND agent_id IS NOT NULL"
        ),
    )

    op.create_index(
        "ix_goals_claimable",
        "goals",
        ["status", "activation_state", "priority", "created_at"],
    )

    op.create_index(
        "ix_hosts_updated_at",
        "hosts",
        [sa.text("updated_at DESC")],
    )

    op.create_index(
        "ix_memories_sweep_candidates",
        "memories",
        ["compartment_id", "last_accessed_at"],
        sqlite_where=sa.text("archived = 0 AND key IS NULL"),
        postgresql_where=sa.text("archived = false AND key IS NULL"),
    )

    op.create_index(
        "ix_connector_oauth_states_expires_unconsumed",
        "connector_oauth_states",
        ["expires_at"],
        sqlite_where=sa.text("consumed_at IS NULL"),
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_index(
        "ix_conversation_items_conv_type_pos",
        "conversation_items",
        ["conversation_id", "type", "position"],
    )

    op.drop_index("ix_workforce_instructions_scope", table_name="workforce_instructions")
    op.drop_index("ix_goal_dependencies_goal", table_name="goal_dependencies")

    _delete_orphaned_child_rows()

    with op.batch_alter_table("comments") as batch_op:
        batch_op.create_foreign_key(
            "fk_comments_conversation_id",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("goal_dependencies") as batch_op:
        batch_op.create_foreign_key(
            "fk_goal_dependencies_goal_id",
            "goals",
            ["goal_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("goal_outcomes") as batch_op:
        batch_op.create_foreign_key(
            "fk_goal_outcomes_goal_id",
            "goals",
            ["goal_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("inbound_event_results") as batch_op:
        batch_op.create_foreign_key(
            "fk_inbound_event_results_idempotency_key",
            "inbound_events",
            ["idempotency_key"],
            ["idempotency_key"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("connector_services") as batch_op:
        batch_op.create_foreign_key(
            "fk_connector_services_connection_id",
            "connector_connections",
            ["connection_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("connector_agent_grants") as batch_op:
        batch_op.create_foreign_key(
            "fk_connector_agent_grants_connection_id",
            "connector_connections",
            ["connection_id"],
            ["id"],
            ondelete="CASCADE",
        )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_conversation_items_search_fts ON conversation_items "
            "USING gin (to_tsvector('english', coalesce(search_text, '')))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_conversation_items_search_fts")

    with op.batch_alter_table("connector_agent_grants") as batch_op:
        batch_op.drop_constraint(
            "fk_connector_agent_grants_connection_id", type_="foreignkey"
        )

    with op.batch_alter_table("connector_services") as batch_op:
        batch_op.drop_constraint("fk_connector_services_connection_id", type_="foreignkey")

    with op.batch_alter_table("inbound_event_results") as batch_op:
        batch_op.drop_constraint(
            "fk_inbound_event_results_idempotency_key", type_="foreignkey"
        )

    with op.batch_alter_table("goal_outcomes") as batch_op:
        batch_op.drop_constraint("fk_goal_outcomes_goal_id", type_="foreignkey")

    with op.batch_alter_table("goal_dependencies") as batch_op:
        batch_op.drop_constraint("fk_goal_dependencies_goal_id", type_="foreignkey")

    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_constraint("fk_comments_conversation_id", type_="foreignkey")

    op.create_index(
        "ix_goal_dependencies_goal",
        "goal_dependencies",
        ["goal_id"],
    )
    op.create_index(
        "ix_workforce_instructions_scope",
        "workforce_instructions",
        ["scope_kind", "scope_id"],
    )

    op.drop_index(
        "ix_conversation_items_conv_type_pos", table_name="conversation_items"
    )
    op.drop_index(
        "ix_connector_oauth_states_expires_unconsumed",
        table_name="connector_oauth_states",
    )
    op.drop_index("ix_memories_sweep_candidates", table_name="memories")
    op.drop_index("ix_hosts_updated_at", table_name="hosts")
    op.drop_index("ix_goals_claimable", table_name="goals")
    op.drop_index("ix_conversations_active_sessions", table_name="conversations")
    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    op.drop_index("ix_conversations_runner_id", table_name="conversations")