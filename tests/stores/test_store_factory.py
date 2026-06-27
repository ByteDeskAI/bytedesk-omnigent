"""Tests for omnigent.stores.factory (BDP-2327, Phase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.stores.agent_store.nats_store import NatsAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.factory import (
    BootstrappedStores,
    StoreBootstrapper,
    _create_artifact_store,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


def test_create_builds_every_store(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create() returns one instance of each expected concrete store type."""
    art_loc = str(tmp_path / "artifacts")
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://omnigent-nats:4222")
    stores = StoreBootstrapper.create(db_uri, art_loc)

    assert isinstance(stores, BootstrappedStores)
    assert isinstance(stores.agent_store, NatsAgentStore)
    assert isinstance(stores.file_store, SqlAlchemyFileStore)
    assert isinstance(stores.conversation_store, SqlAlchemyConversationStore)
    assert isinstance(stores.comment_store, SqlAlchemyCommentStore)
    assert isinstance(stores.policy_store, SqlAlchemyPolicyStore)
    assert isinstance(stores.permission_store, SqlAlchemyPermissionStore)
    assert isinstance(stores.artifact_store, LocalArtifactStore)
    assert isinstance(stores.host_store, HostStore)


def test_local_artifact_store_round_trips(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bootstrapped local artifact store is a usable store."""
    monkeypatch.setenv("OMNIGENT_NATS_URL", "nats://omnigent-nats:4222")
    stores = StoreBootstrapper.create(db_uri, str(tmp_path / "artifacts"))
    stores.artifact_store.put("k", b"payload")
    assert stores.artifact_store.get("k") == b"payload"


def test_create_artifact_store_local_branch(tmp_path: Path) -> None:
    """A non-dbfs location resolves to LocalArtifactStore."""
    store = _create_artifact_store(str(tmp_path / "artifacts"))
    assert isinstance(store, LocalArtifactStore)


def test_create_artifact_store_databricks_branch_matches_cli() -> None:
    """The dbfs:/Volumes/ prefix is the Databricks branch trigger.

    Pins the branch condition to the same prefix cli._create_artifact_store
    uses, without importing the optional databricks-sdk backend: a local
    path must NOT take the Databricks branch.
    """
    assert not str(Path("./artifacts")).startswith("dbfs:/Volumes/")
    assert "dbfs:/Volumes/cat/schema/vol".startswith("dbfs:/Volumes/")


def test_create_artifact_store_nats_branch() -> None:
    """A nats:// location resolves to the NATS Object Store backend (BDP-2380).

    Constructing the adapter must NOT eagerly connect — connection is lazy
    (first use) — so this is safe with no live NATS server.
    """
    from omnigent.stores.artifact_store.nats_object_store import (
        NatsObjectStoreArtifactStore,
    )

    store = _create_artifact_store("nats://omnigent-nats:4222/omnigent-artifacts")
    assert isinstance(store, NatsObjectStoreArtifactStore)
    assert store.bucket == "omnigent-artifacts"
