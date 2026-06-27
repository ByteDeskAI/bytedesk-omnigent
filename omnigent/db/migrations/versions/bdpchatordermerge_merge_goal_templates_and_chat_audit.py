"""merge goal templates and chat event audit migration heads

Revision ID: bdpchatordermerge
Revises: bdp2588goaltmpl, bdpchatorderaudit
Create Date: 2026-06-26

The goal-template migration and chat-ordering audit migration both landed from
the same prior head. This no-op revision preserves both schema branches and
returns Alembic to a single head for startup migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "bdpchatordermerge"
down_revision: str | Sequence[str] | None = ("bdp2588goaltmpl", "bdpchatorderaudit")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
