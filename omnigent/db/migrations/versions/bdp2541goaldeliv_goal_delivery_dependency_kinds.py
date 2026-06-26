"""extend goal_dependencies kinds for goal-delivery DAGs (ADR-0154)

Revision ID: bdp2541goaldeliv
Revises: e1f2a3b4c5d6
Create Date: 2026-06-25

ADR-0154 (BDP-2542) adds milestone/epic/github_pr/jira_issue dependency kinds so
the GoalDeliveryProjector can wire delivery DAGs (a milestone unlocks dependent
goals; a completed Epic goal satisfies ``epic``/``goal`` deps). The persisted
``ck_goal_dependencies_kind`` CHECK only allowed manual/goal/system_state, so the
new kinds must be added to the constraint. Batch mode is required for SQLite,
which can't ALTER a constraint in place — alembic copies the table, swaps the
named CHECK, and renames it back.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bdp2541goaldeliv"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "kind in ('manual', 'goal', 'system_state')"
_NEW = (
    "kind in ('manual', 'goal', 'system_state', "
    "'milestone', 'epic', 'github_pr', 'jira_issue')"
)


def upgrade() -> None:
    with op.batch_alter_table("goal_dependencies") as batch_op:
        batch_op.drop_constraint("ck_goal_dependencies_kind", type_="check")
        batch_op.create_check_constraint("ck_goal_dependencies_kind", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("goal_dependencies") as batch_op:
        batch_op.drop_constraint("ck_goal_dependencies_kind", type_="check")
        batch_op.create_check_constraint("ck_goal_dependencies_kind", _OLD)
