"""add inbound_events + inbound_event_results tables (ADR-0155, BDP-2559)

Revision ID: bdp2557inbound
Revises: bdp2541goaldeliv
Create Date: 2026-06-26

The Wire-Tap Message Store for the generic inbound-event pipeline. ``inbound_events``
is the observable log (one row per inbound event; ``idempotency_key`` PK is the
Idempotent-Receiver guard); ``inbound_event_results`` records per-processor fan-out
outcomes + Dead-Letter retry state. Both additive — no existing table touched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdp2557inbound"
down_revision: str | None = "bdp2541goaldeliv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbound_events",
        sa.Column("idempotency_key", sa.String(length=256), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("event_id", sa.String(length=256), nullable=True),
        sa.Column("occurred_at", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("normalized", sa.Text(), nullable=True),
        sa.Column("headers", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status in ('received', 'fanned_out', 'duplicate', 'dead_lettered')",
            name="ck_inbound_events_status",
        ),
    )
    op.create_index("ix_inbound_events_source_type", "inbound_events", ["source", "type"])
    op.create_index("ix_inbound_events_status_updated", "inbound_events", ["status", "updated_at"])
    op.create_index("ix_inbound_events_received", "inbound_events", ["received_at"])

    op.create_table(
        "inbound_event_results",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("processor", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("idempotency_key", "processor", name="uq_inbound_result"),
        sa.CheckConstraint(
            "status in ('ok', 'skipped', 'failed', 'dead_lettered')",
            name="ck_inbound_event_results_status",
        ),
    )
    op.create_index("ix_inbound_result_retry", "inbound_event_results", ["status", "next_retry_at"])


def downgrade() -> None:
    op.drop_table("inbound_event_results")
    op.drop_table("inbound_events")
