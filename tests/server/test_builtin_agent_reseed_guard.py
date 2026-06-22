"""The startup wheel re-seed must not clobber a migrated (omnigent-SoT) agent."""

from __future__ import annotations

from pathlib import Path

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import _ensure_builtin_agent
from omnigent.server.bundles import bundle_location
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore


def _bundle(tmp_path: Path, harness: str, tag: str) -> bytes:
    image = tmp_path / f"img_{tag}"
    image.mkdir(parents=True, exist_ok=True)
    (image / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: reseed-demo\n"
        "executor:\n"
        "  type: omnigent\n"
        f"  config:\n    harness: {harness}\n"
    )
    return build_bundle_bytes(image)


def _stores(db_uri: str, tmp_path: Path):
    artifact_store = LocalArtifactStore(str(tmp_path / "art"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    return agent_store, artifact_store, agent_cache


def test_migrated_agent_survives_wheel_reseed(db_uri: str, tmp_path: Path) -> None:
    agent_store, artifact_store, agent_cache = _stores(db_uri, tmp_path)
    wheel = _bundle(tmp_path, "claude-sdk", "v1")

    # Initial wheel seed registers the agent.
    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel
    )
    agent = agent_store.get_by_name("reseed-demo")
    assert agent is not None

    # Simulate a runtime edit through PUT /v1/agents/{id}/image: a new
    # bundle is stored, the row repointed, and the agent marked migrated.
    edited = _bundle(tmp_path, "openai-agents", "v2")
    edited_loc = bundle_location(agent.id, edited)
    artifact_store.put(edited_loc, edited)
    agent_store.update(agent.id, edited_loc)
    agent_store.set_sot_tier(agent.id, "migrated")

    # A subsequent boot re-runs the wheel seed with the STALE wheel bundle.
    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel
    )

    # The edit must survive: the row still points at the edited bundle.
    after = agent_store.get_by_name("reseed-demo")
    assert after is not None
    assert after.bundle_location == edited_loc


def test_missing_artifact_is_reseeded_when_db_row_current(
    db_uri: str, tmp_path: Path
) -> None:
    """Self-heal (BDP-2381): a durable DB row + a wiped artifact re-puts.

    Reproduces the durability bug: the agent row is durable in Postgres
    but the bundle lived on an ephemeral emptyDir that a server roll
    wiped. The matching-hash branch must verify the artifact exists and
    re-put it when missing — otherwise the next load fails with
    "unable to load agent spec".
    """
    agent_store, artifact_store, agent_cache = _stores(db_uri, tmp_path)
    wheel = _bundle(tmp_path, "claude-sdk", "v1")

    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel
    )
    agent = agent_store.get_by_name("reseed-demo")
    assert agent is not None
    loc = agent.bundle_location

    # Simulate the ephemeral-volume wipe: the durable DB row survives a
    # roll, but the artifact blob is gone.
    artifact_store.delete(loc)
    assert not artifact_store.exists(loc)

    # A subsequent boot re-runs the wheel seed with the SAME (matching-hash)
    # wheel. It must re-put the missing artifact, not just evict + return.
    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel
    )

    assert artifact_store.exists(loc)
    assert artifact_store.get(loc) == wheel


def test_non_migrated_agent_is_refreshed_by_reseed(db_uri: str, tmp_path: Path) -> None:
    agent_store, artifact_store, agent_cache = _stores(db_uri, tmp_path)
    wheel_v1 = _bundle(tmp_path, "claude-sdk", "v1")

    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel_v1
    )
    agent = agent_store.get_by_name("reseed-demo")
    assert agent is not None

    # A newer wheel ships different content; the agent is NOT migrated, so
    # the re-seed should refresh it to the new bundle.
    wheel_v2 = _bundle(tmp_path, "openai-agents", "v2")
    _ensure_builtin_agent(
        agent_store, artifact_store, agent_cache, name="reseed-demo", bundle_bytes=wheel_v2
    )

    after = agent_store.get_by_name("reseed-demo")
    assert after is not None
    assert after.bundle_location == bundle_location(agent.id, wheel_v2)
