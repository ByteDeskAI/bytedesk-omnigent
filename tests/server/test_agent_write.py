"""Tests for the shared apply_bundle_update helper."""

from __future__ import annotations

import io
import tarfile

import pytest
import yaml

from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.bundles import bundle_location
from omnigent.stores.agent_definition_store import head_key


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.blobs[key] = data


class _FakeAgentStore:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self.updates: list[tuple[str, str]] = []

    def update(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expected_version: int | None = None,
    ) -> Agent | None:
        self.updates.append((agent_id, bundle_location))
        self._agent.bundle_location = bundle_location
        self._agent.version += 1
        return self._agent


class _FakeAgentCache:
    def __init__(self) -> None:
        self.replaced: list[tuple[str, str, bool]] = []

    def replace(self, agent_id, bundle_location, bundle_bytes, *, expand_env):
        self.replaced.append((agent_id, bundle_location, expand_env))


def _agent(bundle_location_value: str) -> Agent:
    return Agent(
        id="ag_test",
        created_at=0,
        name="demo",
        bundle_location=bundle_location_value,
        version=1,
        session_id=None,
    )


def _bundle_bytes(description: str = "demo") -> bytes:
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "demo",
            "description": description,
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        }
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        data = config.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_changed_content_stores_updates_and_warm_swaps() -> None:
    new_bytes = _bundle_bytes("new")
    agent = _agent("ag_test/oldhash")
    art, store, cache = _FakeArtifactStore(), None, _FakeAgentCache()
    store = _FakeAgentStore(agent)

    result = apply_bundle_update(
        agent,
        new_bytes,
        artifact_store=art,
        agent_store=store,
        agent_cache=cache,
        expand_env=True,
    )

    expected_loc = bundle_location(agent.id, new_bytes)
    assert expected_loc in art.blobs
    assert head_key(agent.id) in art.blobs
    assert store.updates == [("ag_test", expected_loc)]
    assert cache.replaced == [("ag_test", expected_loc, True)]
    assert result.bundle_location == expected_loc
    assert result.version == 2


def test_identical_content_is_noop() -> None:
    same_bytes = _bundle_bytes("unchanged")
    agent = _agent(bundle_location("ag_test", same_bytes))
    art, store, cache = _FakeArtifactStore(), _FakeAgentStore(_agent("x")), _FakeAgentCache()

    result = apply_bundle_update(
        agent,
        same_bytes,
        artifact_store=art,
        agent_store=store,
        agent_cache=cache,
        expand_env=True,
    )

    assert result is agent
    assert art.blobs == {}
    assert store.updates == []
    assert cache.replaced == []


def test_write_without_artifact_store_raises() -> None:
    agent = _agent("ag_test/oldhash")
    with pytest.raises(OmnigentError):
        apply_bundle_update(
            agent,
            _bundle_bytes("new"),
            artifact_store=None,
            agent_store=_FakeAgentStore(agent),
            agent_cache=None,
            expand_env=True,
        )


class _VanishedAgentStore(_FakeAgentStore):
    def update(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expected_version: int | None = None,
    ) -> Agent | None:
        self.updates.append((agent_id, bundle_location))
        return None


def test_agent_vanished_mid_update_raises_not_found() -> None:
    agent = _agent("ag_test/oldhash")
    with pytest.raises(OmnigentError, match="Agent not found"):
        apply_bundle_update(
            agent,
            _bundle_bytes("new"),
            artifact_store=_FakeArtifactStore(),
            agent_store=_VanishedAgentStore(agent),
            agent_cache=None,
            expand_env=True,
        )
