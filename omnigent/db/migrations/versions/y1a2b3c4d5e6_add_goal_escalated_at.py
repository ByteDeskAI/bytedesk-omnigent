"""add goals.escalated_at (BDP-2283 C4 escalation dedup, ADR-0142)

Revision ID: y1a2b3c4d5e6
Revises: x1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

The accountability loop (C4) re-escalated every blocked goal on every tick.
``escalated_at`` is the dedup marker: set when a blocked goal is escalated, reset
to NULL on every (re-)transition to 'blocked', so a goal escalates once per
blocked episode. Nullable Integer (epoch seconds) — dialect-neutral.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y1a2b3c4d5e6"
down_revision: str | None = "x1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("escalated_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("goals", "escalated_at")
