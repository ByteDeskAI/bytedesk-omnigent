"""Per-agent tier classification on the store (agent-tiering step 1).

``category`` is the queryable tier column written by the post-seed backfill;
``list(category=...)`` filters on it and still honors the template-only
(``session_id IS NULL``) restriction.
"""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.db.utils import generate_agent_id, get_or_create_engine
from omnigent.entities import SystemAgent, Workflow
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def test_category_column_exists(tmp_path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    cols = {c["name"] for c in sa.inspect(engine).get_columns("agents")}
    assert "category" in cols


def test_category_defaults_none_and_round_trips(tmp_path) -> None:
    store = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'a.db'}")
    agent_id = generate_agent_id()
    store.create(agent_id, name="some-orchestrator", bundle_location="x:///b")

    # create() does not set it — NULL until the backfill classifies.
    assert store.get_category(agent_id) is None

    assert store.set_category(agent_id, "workflow") is True
    assert store.get_category(agent_id) == "workflow"
    # The entity now resolves to the right concrete via the converter.
    assert isinstance(store.get(agent_id), Workflow)

    assert store.set_category(agent_id, None) is True
    assert store.get_category(agent_id) is None


def test_set_category_unknown_agent_returns_false(tmp_path) -> None:
    store = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'a.db'}")
    assert store.set_category("ag_missing", "system") is False
    assert store.get_category("ag_missing") is None


def test_list_filters_by_category(tmp_path) -> None:
    store = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'a.db'}")
    sys_id, wf_id, emp_id = generate_agent_id(), generate_agent_id(), generate_agent_id()
    store.create(sys_id, name="skill-manager", bundle_location="x:///s")
    store.create(wf_id, name="weekly-report", bundle_location="x:///w")
    store.create(emp_id, name="vivian", bundle_location="x:///e")
    store.set_category(sys_id, "system")
    store.set_category(wf_id, "workflow")
    store.set_category(emp_id, "employee")

    workflows = store.list(limit=100, category="workflow")
    assert [a.id for a in workflows.data] == [wf_id]
    assert isinstance(workflows.data[0], Workflow)

    systems = store.list(limit=100, category="system")
    assert [a.id for a in systems.data] == [sys_id]
    assert isinstance(systems.data[0], SystemAgent)

    # No filter → all three tiers.
    everyone = {a.id for a in store.list(limit=100).data}
    assert everyone == {sys_id, wf_id, emp_id}
