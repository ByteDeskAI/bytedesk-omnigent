"""The post-seed classification backfill (agent-tiering step 1/2).

``_backfill_agent_classification`` persists each template agent's ``category``
column AND materializes the spec's declared ``capabilities:`` onto the row, so
the route-level skill-manage authz gate (BDP-2577) can read them back. These
tests drive the pass directly against real stores rather than a full server
seed.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import _backfill_agent_classification
from omnigent.server.bundles import bundle_location
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore

_BASE = (
    "spec_version: 1\n"
    "name: {name}\n"
    "description: x\n"
    "executor:\n"
    "  type: omnigent\n"
    "  config:\n"
    "    harness: claude-sdk\n"
)


def _seed(db_uri: str, tmp_path: Path, *, name: str, extra: str = "") -> str:
    image = tmp_path / f"img_{name}"
    image.mkdir(parents=True)
    (image / "config.yaml").write_text(_BASE.format(name=name) + extra)
    (image / "AGENTS.md").write_text("You are an agent.\n")
    bundle_bytes = build_bundle_bytes(image)
    agent_id = generate_agent_id()
    loc = bundle_location(agent_id, bundle_bytes)
    LocalArtifactStore(str(tmp_path / "artifacts")).put(loc, bundle_bytes)
    SqlAlchemyAgentStore(db_uri).create(agent_id, name=name, bundle_location=loc)
    return agent_id


def _stores(db_uri: str, tmp_path: Path) -> tuple[SqlAlchemyAgentStore, AgentCache]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return SqlAlchemyAgentStore(db_uri), AgentCache(
        artifact_store=artifact_store, cache_dir=tmp_path / "cache"
    )


def test_backfill_classifies_and_syncs_capabilities(tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'a.db'}"
    concierge = _seed(
        db_uri,
        tmp_path,
        name="skills-concierge",
        extra="capabilities:\n  - system.skills.manage\n",
    )
    goal_commander = _seed(
        db_uri,
        tmp_path,
        name="goal-commander",
        extra="params:\n  department: Operations\n",
    )
    workflow = _seed(db_uri, tmp_path, name="weekly-report", extra="params:\n  workflow: true\n")
    employee = _seed(db_uri, tmp_path, name="vivian")

    store, cache = _stores(db_uri, tmp_path)
    _backfill_agent_classification(store, cache)

    # Category column written for all three tiers.
    assert store.get_category(concierge) == "system"
    assert store.get_category(goal_commander) == "system"
    assert store.get_category(workflow) == "workflow"
    assert store.get_category(employee) == "employee"

    # The system agent's declared capability is materialized onto the row so the
    # authz gate can read it; agents that declare none stay empty.
    assert "system.skills.manage" in store.get_capabilities(concierge)
    assert store.get_capabilities(employee) == ()


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'a.db'}"
    concierge = _seed(
        db_uri,
        tmp_path,
        name="skills-concierge",
        extra="capabilities:\n  - system.skills.manage\n",
    )

    store, cache = _stores(db_uri, tmp_path)
    _backfill_agent_classification(store, cache)
    _backfill_agent_classification(store, cache)  # second pass is a no-op

    assert store.get_category(concierge) == "system"
    assert store.get_capabilities(concierge) == ("system.skills.manage",)
