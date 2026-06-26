"""add category (agent tier) to agents

Revision ID: bdpagentcategory
Revises: bdp2557inbound
Create Date: 2026-06-26 00:00:00.000000

Adds the first-class agent-tier classification to the ``agents`` table: a short
string column ``category`` (``"system" | "employee" | "workflow"``) so the three
tiers are queryable (``/v1/agents?category=``, admin surfaces) without loading
every spec. Stored as ``sa.String(16)``, dual-DB safe (SQLite + Postgres),
indexed. NULL = not yet classified — the converter falls back to name-only
inference and the post-seed backfill (``_ensure_default_agents``) writes the
authoritative value (including ``workflow``, which needs the spec).

Backfill here is the cheap, deterministic part: allowlisted system-agent ids →
``"system"``; everything else → ``"employee"``. Workflow rows can't be detected
from SQL (the flag lives in the bundle ``params``), so they land as ``employee``
and are corrected to ``"workflow"`` on the next startup seed — a one-boot window.
SQLite-safe via batch mode. Mirrors the ``sot_tier`` / ``capabilities`` adds.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdpagentcategory"
down_revision: str | None = "bdp2557inbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from omnigent.db.utils import builtin_agent_id
    from omnigent.entities.automation import SYSTEM_AGENT_NAMES

    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("category", sa.String(length=16), nullable=True))
        batch_op.create_index("ix_agents_category", ["category"])

    agents = sa.table("agents", sa.column("id"), sa.column("category"))
    conn = op.get_bind()
    system_ids = [builtin_agent_id(name) for name in SYSTEM_AGENT_NAMES]
    conn.execute(
        agents.update().where(agents.c.id.in_(system_ids)).values(category="system")
    )
    conn.execute(
        agents.update().where(agents.c.category.is_(None)).values(category="employee")
    )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_index("ix_agents_category")
        batch_op.drop_column("category")
