"""add sot_tier migration marker to agents (FU3/FU5, BDP-2149)

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-17 00:00:00.000000

Adds the per-agent migration tier marker to the ``agents`` table (ADR-0133 /
ADR-0136): NULL = OpenClaw-resident (default), ``"migrated"`` = omnigent is the
source of truth for this agent's domains. ``params`` are baked at YAML-parse
time and immutable at runtime, so the flip-able cutover marker must live on a
mutable column written via ``AgentStore.set_sot_tier`` — never inferred from
registry presence. SQLite-safe via batch mode.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("sot_tier", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("sot_tier")
