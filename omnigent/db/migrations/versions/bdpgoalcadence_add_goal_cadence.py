"""add goal cadence columns (BDP-2583)

Revision ID: bdpgoalcadence
Revises: bdpfabricoutbox
Create Date: 2026-06-26 00:00:00.000000

A goal can now declare a dispatch cadence: ``immediate`` (dispatch once when
ready — the default, so existing goals are unchanged) or ``recurring`` /
``until_done`` (a cron ``cadence_expr`` re-dispatches the goal on a schedule).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdpgoalcadence"
down_revision: str | None = "bdpfabricoutbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cadence_kind",
                sa.String(length=16),
                nullable=False,
                server_default="immediate",
            )
        )
        batch_op.add_column(sa.Column("cadence_expr", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("cadence_tz", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.drop_column("cadence_tz")
        batch_op.drop_column("cadence_expr")
        batch_op.drop_column("cadence_kind")
