"""Phase 6a admin CRUD store methods (BDP-2588): delete, remove-dependency,
conditions round-trip + validation, templates CRUD + instantiate, and the
entity.changed delta emitted for each new mutation type."""
from __future__ import annotations

import bytedesk_omnigent.goals as goals_mod
from bytedesk_omnigent.goals import (
    GoalTemplateStore,
    SqlAlchemyGoalStore,
)


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _capture_goal_events(monkeypatch) -> list[tuple]:
    events: list[tuple] = []
    monkeypatch.setattr(
        goals_mod,
        "_publish_goal_event",
        lambda change, goal, **kw: events.append((change, goal.id)),
    )
    return events


def _capture_entity_events(monkeypatch) -> list[tuple]:
    events: list[tuple] = []
    monkeypatch.setattr(
        goals_mod,
        "_publish_entity_event",
        lambda entity, op, entity_id, **kw: events.append((entity, op, entity_id)),
    )
    return events


def test_delete_goal_removes_row_and_dependencies_and_emits(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="ship",
        dependencies=[{"kind": "manual", "label": "do thing"}],
        now=100,
    )
    events = _capture_goal_events(monkeypatch)

    assert store.delete_goal(goal_id=goal.id, now=200) is True
    assert store.get_goal(goal_id=goal.id) is None
    assert store.delete_goal(goal_id=goal.id, now=201) is False  # already gone
    assert ("deleted", goal.id) in events


def test_remove_dependency_recomputes_readiness_and_emits(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(
        title="reporting",
        dependencies=[{"kind": "system_state", "label": "export ready"}],
        now=100,
    )
    assert goal.activation_state == "waiting"
    dep_id = goal.dependencies[0].id
    events = _capture_goal_events(monkeypatch)

    assert store.remove_dependency(goal_id=goal.id, dependency_id=dep_id, now=200) is True
    refreshed = store.get_goal(goal_id=goal.id)
    assert refreshed is not None
    assert refreshed.dependencies == ()
    # a 'dependent' goal with zero deps stays waiting (existing _activation_for
    # semantics) until explicitly activated; readiness was recomputed.
    assert refreshed.activation_state == "waiting"
    assert ("dependency_removed", goal.id) in events
    # removing an unknown dep is a no-op False
    assert store.remove_dependency(goal_id=goal.id, dependency_id="nope", now=201) is False


def test_condition_get_put_roundtrip_and_emit(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="conditional", now=100)
    assert store.get_condition(goal_id=goal.id) is None
    events = _capture_entity_events(monkeypatch)

    ast = {
        "type": "leaf",
        "sensor": "jira",
        "query": {"key": "BDP-1"},
        "predicate": {"op": "exists"},
    }
    store.set_condition(goal_id=goal.id, ast_dict=ast, now=200)
    assert store.get_condition(goal_id=goal.id) == ast
    assert ("condition", "set", goal.id) in events

    store.set_condition(goal_id=goal.id, ast_dict=None, now=201)
    assert store.get_condition(goal_id=goal.id) is None
    assert ("condition", "deleted", goal.id) in events


def test_set_condition_rejects_bad_ast(tmp_path) -> None:
    store = _store(tmp_path)
    goal = store.create_goal(title="bad", now=100)
    bad = {"type": "leaf", "sensor": "x", "query": {}, "predicate": {"op": "wat"}}
    try:
        store.set_condition(goal_id=goal.id, ast_dict=bad, now=200)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for invalid predicate op")
    # nothing persisted
    assert store.get_condition(goal_id=goal.id) is None


def _tmpl_store(tmp_path) -> GoalTemplateStore:
    goal_store = _store(tmp_path)
    return GoalTemplateStore(f"sqlite:///{tmp_path / 'goals.db'}", goal_store)


def test_template_crud_and_emit(tmp_path, monkeypatch) -> None:
    store = _tmpl_store(tmp_path)
    events = _capture_entity_events(monkeypatch)

    t = store.create_template(
        name="weekly-review",
        description="recurring ops review",
        definition={"priority": 2, "risk_tier": "medium"},
        now=100,
    )
    assert next(x for x in events if x[0] == "template") == ("template", "created", t.id)
    assert store.get_template(template_id=t.id).definition["priority"] == 2
    assert [x.id for x in store.list_templates()] == [t.id]

    updated = store.update_template(
        template_id=t.id, definition={"priority": 5}, now=101
    )
    assert updated.definition == {"priority": 5}
    assert ("template", "updated", t.id) in events

    assert store.delete_template(template_id=t.id) is True
    assert store.get_template(template_id=t.id) is None
    assert ("template", "deleted", t.id) in events
    assert store.delete_template(template_id=t.id) is False


def test_template_instantiate_creates_goal_from_blueprint(tmp_path) -> None:
    goal_store = _store(tmp_path)
    store = GoalTemplateStore(f"sqlite:///{tmp_path / 'goals.db'}", goal_store)
    ast = {
        "type": "leaf",
        "sensor": "jira",
        "query": {"key": "BDP-1"},
        "predicate": {"op": "exists"},
    }
    t = store.create_template(
        name="blueprint",
        definition={
            "priority": 4,
            "target_kind": "department",
            "target_id": "Operations",
            "risk_tier": "high",
            "conditions": ast,
        },
        now=100,
    )
    goal = store.instantiate(
        template_id=t.id, overrides={"title": "From template", "priority": 1}, now=200
    )
    assert goal is not None
    assert goal.title == "From template"
    assert goal.priority == 1  # override wins over definition
    assert goal.target_kind == "department"
    assert goal.risk_tier == "high"
    assert (goal.payload or {}).get("condition") == ast
    # default title falls back to template name
    goal2 = store.instantiate(template_id=t.id, now=201)
    assert goal2.title == "blueprint"
    # unknown template
    assert store.instantiate(template_id="missing", now=202) is None
