"""Tests for the Condition AST — predicates, boolean nodes, JSON round-trip (BDP-2584)."""
from __future__ import annotations

from bytedesk_omnigent.engine.conditions import (
    All,
    Any,
    Leaf,
    Not,
    Predicate,
    from_dict,
)


def _reading(satisfied: bool, value=None):
    return {"satisfied": satisfied, "value": value, "observed_at": 0, "stale_after_s": None}


# -- predicates --------------------------------------------------------------
def test_predicate_exists_uses_satisfied_flag() -> None:
    leaf = Leaf(sensor="manual", query={"dep_id": "d1"}, predicate=Predicate("exists"))
    assert leaf.eval({"manual:dep_id=d1": _reading(True)}) is True
    assert leaf.eval({"manual:dep_id=d1": _reading(False)}) is False


def test_predicate_equals_gt_lt_contains_on_value() -> None:
    key = "goal_outcome:goal_id=g1"
    eq = Leaf("goal_outcome", {"goal_id": "g1"}, Predicate("equals", "done"))
    gt = Leaf("goal_outcome", {"goal_id": "g1"}, Predicate("gt", 5))
    lt = Leaf("goal_outcome", {"goal_id": "g1"}, Predicate("lt", 5))
    contains = Leaf("goal_outcome", {"goal_id": "g1"}, Predicate("contains", "ship"))

    assert eq.eval({key: _reading(True, "done")}) is True
    assert eq.eval({key: _reading(True, "open")}) is False
    assert gt.eval({key: _reading(True, 9)}) is True
    assert gt.eval({key: _reading(True, 1)}) is False
    assert lt.eval({key: _reading(True, 1)}) is True
    assert contains.eval({key: _reading(True, "we ship it")}) is True
    assert contains.eval({key: _reading(True, ["ship", "it"])}) is True
    assert contains.eval({key: _reading(True, "nope")}) is False


def test_missing_reading_is_false() -> None:
    leaf = Leaf("manual", {"dep_id": "d1"}, Predicate("exists"))
    assert leaf.eval({}) is False


# -- boolean nodes -----------------------------------------------------------
def _readings(**kv):
    return {k: _reading(v) for k, v in kv.items()}


def test_all_any_not() -> None:
    a = Leaf("manual", {"dep_id": "a"}, Predicate("exists"))
    b = Leaf("manual", {"dep_id": "b"}, Predicate("exists"))
    readings = {"manual:dep_id=a": _reading(True), "manual:dep_id=b": _reading(False)}

    assert All([a, b]).eval(readings) is False
    assert Any([a, b]).eval(readings) is True
    assert Not(b).eval(readings) is True
    assert All([a]).eval(readings) is True


def test_leaves_walks_tree() -> None:
    a = Leaf("manual", {"dep_id": "a"}, Predicate("exists"))
    b = Leaf("goal_outcome", {"goal_id": "g"}, Predicate("equals", "done"))
    tree = All([a, Not(Any([b]))])
    leaves = tree.leaves()
    assert {leaf.sensor for leaf in leaves} == {"manual", "goal_outcome"}
    assert len(leaves) == 2


# -- JSON round-trip ---------------------------------------------------------
def test_json_round_trip() -> None:
    tree = All(
        [
            Leaf("manual", {"dep_id": "a"}, Predicate("exists")),
            Any(
                [
                    Leaf("goal_outcome", {"goal_id": "g"}, Predicate("equals", "done")),
                    Not(Leaf("time", {"after": 100}, Predicate("exists"))),
                ]
            ),
        ]
    )
    restored = from_dict(tree.to_dict())
    assert restored.to_dict() == tree.to_dict()
    # and it still evaluates
    readings = {
        "manual:dep_id=a": _reading(True),
        "goal_outcome:goal_id=g": _reading(True, "done"),
        "time:after=100": _reading(False),
    }
    assert restored.eval(readings) == tree.eval(readings)
