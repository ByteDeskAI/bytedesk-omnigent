"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

from omnigent.db.db_models import SqlAgent
from omnigent.entities import Agent, Automation, SystemAgent, Workflow, infer_category

# Tier (category string) → concrete entity class. The class IS the discriminator
# (house idiom: spec/types.py PolicySpec subclasses).
_BY_CATEGORY: dict[str, type[Automation]] = {
    "system": SystemAgent,
    "employee": Agent,
    "workflow": Workflow,
}


def sql_agent_to_entity(row: SqlAgent) -> Automation:
    """
    Convert a :class:`SqlAgent` ORM row to the right :class:`Automation` concrete.

    Dispatches on the persisted ``category`` column; for rows written before the
    column was populated it falls back to name-only inference
    (:func:`~omnigent.entities.infer_category` with ``params=None``), which
    resolves ``system``/``employee`` but defaults ``workflow`` rows to ``Agent``
    until the post-seed backfill persists their column.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`SystemAgent`, :class:`Agent`, or :class:`Workflow`.
    """
    category = row.category or infer_category(row.name, None)
    cls = _BY_CATEGORY.get(category, Agent)
    return cls(
        id=row.id,
        created_at=row.created_at,
        name=row.name,
        bundle_location=row.bundle_location,
        version=row.version,
        description=row.description,
        updated_at=row.updated_at,
        session_id=row.session_id,
    )
