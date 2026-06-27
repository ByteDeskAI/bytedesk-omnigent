"""Per-tenant goal-attribute schema validation (BDP-2589, Phase 7).

When a tenant configures an attribute schema, ``create_goal``/``update_goal``
validate ``payload["attributes"]`` against it. No schema → anything allowed
(back-compat: existing goals carry free-form attributes).
"""
from __future__ import annotations

import pytest

from bytedesk_omnigent.engine.config import validate_goal_attributes


def _schema():
    # JSON-schema-ish: allowed attribute names + their required type.
    return {
        "properties": {
            "paper_trading": {"type": "boolean"},
            "campaign": {"type": "string"},
        },
        "additionalProperties": False,
    }


def test_absent_schema_allows_anything() -> None:
    validate_goal_attributes({"anything": 1, "wild": [1, 2]}, schema=None)  # no raise


def test_schema_rejects_unknown_attribute() -> None:
    with pytest.raises(ValueError, match="unknown"):
        validate_goal_attributes({"nope": 1}, schema=_schema())


def test_schema_rejects_wrong_type() -> None:
    with pytest.raises(ValueError, match="type"):
        validate_goal_attributes({"campaign": 123}, schema=_schema())


def test_schema_accepts_valid() -> None:
    validate_goal_attributes(
        {"paper_trading": True, "campaign": "q3"}, schema=_schema()
    )  # no raise


def test_create_goal_validates_when_schema_configured(tmp_path) -> None:
    from bytedesk_omnigent.goals import SqlAlchemyGoalStore

    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    # Inject the per-tenant schema resolver so create_goal enforces it.
    store.set_attribute_schema_resolver(lambda target_id: _schema())
    with pytest.raises(ValueError, match="unknown"):
        store.create_goal(title="bad", payload={"attributes": {"nope": 1}})
    # Valid attributes pass.
    g = store.create_goal(title="ok", payload={"attributes": {"campaign": "q3"}})
    assert g.attributes["campaign"] == "q3"


def test_create_goal_back_compat_without_resolver(tmp_path) -> None:
    from bytedesk_omnigent.goals import SqlAlchemyGoalStore

    store = SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")
    # No resolver configured → free-form attributes allowed (existing behaviour).
    g = store.create_goal(title="ok", payload={"attributes": {"wild": [1, 2, 3]}})
    assert g.attributes["wild"] == [1, 2, 3]
