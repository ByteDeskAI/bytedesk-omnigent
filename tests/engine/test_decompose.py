"""Auto-decomposition (BDP-2596 Wave 3, feature 5).

An org/dept goal is decomposed into child goals (parent_goal_id tree) with
inherited constraints (budget scope / risk / deadline). Outcome roll-up already
exists in the tick. The planner agent supplies the spec; the engine wires the
tree + inheritance. Fakes only — the deterministic spec path.
"""
from __future__ import annotations

from bytedesk_omnigent.engine.decompose import decompose_goal
from bytedesk_omnigent.goals import SqlAlchemyGoalStore


def _store(tmp_path):
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'g.db'}")


def test_decompose_creates_children_linked_to_parent(tmp_path) -> None:
    store = _store(tmp_path)
    parent = store.create_goal(
        title="launch", target_kind="organization",
        expected_value_cents=9000, risk_tier="medium",
    )
    children = decompose_goal(
        store, parent_goal_id=parent.id,
        spec=[{"title": "design"}, {"title": "build"}, {"title": "ship"}],
    )
    assert len(children) == 3
    for child in children:
        refreshed = store.get_goal(goal_id=child.id)
        assert refreshed.parent_goal_id == parent.id


def test_children_inherit_parent_constraints(tmp_path) -> None:
    store = _store(tmp_path)
    parent = store.create_goal(
        title="p", target_kind="department", target_id="dept_eng",
        risk_tier="high", expected_value_cents=10_000,
    )
    children = decompose_goal(
        store, parent_goal_id=parent.id, spec=[{"title": "a"}, {"title": "b"}],
    )
    for child in children:
        c = store.get_goal(goal_id=child.id)
        # inherited risk + scope from the parent.
        assert c.risk_tier == "high"
        assert c.tier == parent.tier
        assert c.target_id == parent.target_id


def test_child_overrides_win_over_inheritance(tmp_path) -> None:
    store = _store(tmp_path)
    parent = store.create_goal(title="p", risk_tier="high", expected_value_cents=4000)
    children = decompose_goal(
        store, parent_goal_id=parent.id,
        spec=[{"title": "low-risk subtask", "risk_tier": "low", "priority": 1}],
    )
    c = store.get_goal(goal_id=children[0].id)
    assert c.risk_tier == "low"  # explicit override beats inheritance
    assert c.priority == 1


def test_decompose_missing_parent_raises(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        decompose_goal(store, parent_goal_id="nope", spec=[{"title": "x"}])
    except ValueError:
        return
    raise AssertionError("expected ValueError for missing parent")


def test_decompose_empty_spec_is_noop(tmp_path) -> None:
    store = _store(tmp_path)
    parent = store.create_goal(title="p")
    assert decompose_goal(store, parent_goal_id=parent.id, spec=[]) == []


def test_child_value_rolls_up_to_parent_via_tick(tmp_path) -> None:
    # The roll-up itself lives in the loop (already tested there); this proves the
    # tree decompose builds is the same shape the roll-up consumes.
    from bytedesk_omnigent.engine.loop import _roll_up_child_outcomes
    from bytedesk_omnigent.engine.treasury import SqlAlchemyTreasury

    loc = f"sqlite:///{tmp_path / 'g.db'}"
    store = SqlAlchemyGoalStore(loc)
    treasury = SqlAlchemyTreasury(loc)
    parent = store.create_goal(title="p", expected_value_cents=0)
    children = decompose_goal(
        store, parent_goal_id=parent.id, spec=[{"title": "a"}, {"title": "b"}],
    )
    treasury.book_outcome(
        goal_store=store, goal_id=children[0].id, realized_value_cents=120,
        source="rail", evidence=None,
    )
    _roll_up_child_outcomes(store, now=500)
    assert store.get_goal(goal_id=parent.id).realized_value_cents == 120
