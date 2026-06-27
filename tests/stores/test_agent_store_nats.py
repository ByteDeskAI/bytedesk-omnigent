"""NATS AgentStore behavior without a live NATS server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.errors import StaleWriteError
from omnigent.stores.agent_store import events as agent_events
from omnigent.stores.agent_store.import_sql import import_sql_agents
from omnigent.stores.agent_store.nats_store import NatsAgentStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class _NotFound(Exception):
    pass


@dataclass(frozen=True)
class _Entry:
    value: bytes
    revision: int


class _FakeKV:
    def __init__(self) -> None:
        self._current: dict[str, _Entry] = {}
        self._history: dict[str, list[_Entry]] = {}
        self._revision = 0

    async def create(self, key: str, value: bytes) -> _Entry:
        if key in self._current:
            raise ValueError(f"key already exists: {key}")
        return await self._put_live(key, value)

    async def update(self, key: str, value: bytes, *, last: int) -> _Entry:
        current = self._current.get(key)
        if current is None:
            raise _NotFound(key)
        if current.revision != last:
            raise ValueError("stale revision")
        return await self._put_live(key, value)

    async def get(self, key: str) -> _Entry:
        try:
            return self._current[key]
        except KeyError:
            raise _NotFound(key) from None

    async def delete(self, key: str) -> None:
        if key not in self._current:
            raise _NotFound(key)
        del self._current[key]

    async def keys(self) -> list[str]:
        return list(self._current)

    async def history(self, key: str) -> list[_Entry]:
        entries = self._history.get(key)
        if not entries:
            raise _NotFound(key)
        return list(entries)

    async def _put_live(self, key: str, value: bytes) -> _Entry:
        self._revision += 1
        entry = _Entry(value=value, revision=self._revision)
        self._current[key] = entry
        self._history.setdefault(key, []).append(entry)
        return entry


class _FakeJetStream:
    def __init__(self) -> None:
        self.buckets: dict[str, _FakeKV] = {}

    async def key_value(self, bucket: str) -> _FakeKV:
        try:
            return self.buckets[bucket]
        except KeyError:
            raise _NotFound(bucket) from None

    async def create_key_value(
        self,
        *,
        config: Any = None,
        bucket: str | None = None,
        history: int = 1,
    ) -> _FakeKV:
        del history
        name = bucket
        if config is not None:
            name = getattr(config, "bucket", None)
            if name is None and isinstance(config, dict):
                name = config["bucket"]
        assert name is not None
        kv = _FakeKV()
        self.buckets[name] = kv
        return kv


class _FakeNats:
    def __init__(self) -> None:
        self.js = _FakeJetStream()
        self.published: list[tuple[str, bytes]] = []

    def jetstream(self) -> _FakeJetStream:
        return self.js

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))


def _store() -> tuple[NatsAgentStore, _FakeNats]:
    nc = _FakeNats()

    async def connector() -> tuple[_FakeNats, type[BaseException]]:
        return nc, _NotFound

    agent_events.reset_for_test()
    return NatsAgentStore("nats://omnigent-nats:4222", connector=connector), nc


def test_create_get_list_update_revision_and_rollback() -> None:
    store, nc = _store()
    events = []
    agent_events.subscribe(events.append)
    try:
        created = store.create("ag_alpha", "alpha", "ag_alpha/v1", "first")
        assert created.version == 1
        assert store.get("ag_alpha").bundle_location == "ag_alpha/v1"
        assert store.get_by_name("alpha").id == "ag_alpha"
        assert [agent.id for agent in store.list(order="asc").data] == ["ag_alpha"]

        assert store.set_capabilities("ag_alpha", ("office.chat", "files")) is True
        updated = store.update("ag_alpha", "ag_alpha/v2", expected_version=1)
        assert updated.version == 2

        revisions = store.list_revisions("ag_alpha")
        assert len(revisions) == 3
        assert revisions[1].metadata["capabilities"] == ["office.chat", "files"]
        assert "capabilities" in store.diff_revisions(
            "ag_alpha", revisions[0].revision, revisions[1].revision
        )

        rolled_back = store.rollback(
            "ag_alpha", revisions[0].revision, expected_version=2
        )
        assert rolled_back.version == 3
        assert rolled_back.bundle_location == "ag_alpha/v1"
        assert store.get_capabilities("ag_alpha") == ()
    finally:
        agent_events.reset_for_test()

    assert nc.published
    assert [event.action for event in events] == ["created", "updated", "updated", "updated"]


def test_session_scoped_agents_are_indexed_by_session_not_name() -> None:
    store, _nc = _store()

    store.create("ag_template", "alpha", "ag_template/v1")
    store.create("ag_session", "alpha-session", "ag_session/v1", session_id="conv_1")

    assert store.get_by_name("alpha-session") is None
    assert [agent.id for agent in store.list(limit=10, order="asc").data] == ["ag_template"]

    replacement = store.create(
        "ag_session_2",
        "alpha-session-v2",
        "ag_session_2/v1",
        session_id="conv_1",
        replace_session=True,
    )

    assert replacement.session_id == "conv_1"
    assert store.get("ag_session") is None
    assert store.get("ag_session_2").session_id == "conv_1"


def test_guarded_update_rejects_stale_version() -> None:
    store, _nc = _store()
    store.create("ag_etag", "etag", "ag_etag/v1")
    store.update("ag_etag", "ag_etag/v2", expected_version=1)

    with pytest.raises(StaleWriteError):
        store.update("ag_etag", "ag_etag/v3", expected_version=1)

    assert store.get("ag_etag").bundle_location == "ag_etag/v2"


def test_import_sql_agents_copies_legacy_rows(tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'legacy.db'}"
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation()
    source = SqlAlchemyAgentStore(db_uri)
    source.create(
        "ag_import",
        "legacy",
        "ag_import/v1",
        "legacy row",
        session_id=conversation.id,
    )
    source.set_capabilities("ag_import", ("office.chat",))
    source.set_sot_tier("ag_import", "migrated")
    source.set_category("ag_import", "workflow")
    target, _nc = _store()

    report = import_sql_agents(db_uri, target)

    assert report.imported == 1
    assert report.skipped == 0
    assert report.conflicts == []
    imported = target.get("ag_import")
    assert imported.session_id == conversation.id
    assert imported.bundle_location == "ag_import/v1"
    assert target.get_capabilities("ag_import") == ("office.chat",)
    assert target.get_sot_tier("ag_import") == "migrated"
    assert target.get_category("ag_import") == "workflow"

    second = import_sql_agents(db_uri, target)
    assert second.imported == 0
    assert second.skipped == 1
    assert second.conflicts == []


def test_import_sql_agents_reports_conflicts(tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'legacy.db'}"
    source = SqlAlchemyAgentStore(db_uri)
    source.create("ag_conflict", "legacy", "ag_conflict/v1")
    target, _nc = _store()
    target.create("ag_conflict", "legacy", "ag_conflict/different")

    report = import_sql_agents(db_uri, target)

    assert report.imported == 0
    assert report.skipped == 0
    assert report.conflicts == ["ag_conflict"]
