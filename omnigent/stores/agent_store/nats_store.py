"""NATS JetStream-backed AgentStore."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any, TypeVar
from urllib.parse import urlsplit

from omnigent.entities import (
    Agent,
    Automation,
    HarnessAgent,
    PagedList,
    SystemAgent,
    Workflow,
    infer_category,
)
from omnigent.errors import StaleWriteError
from omnigent.stores.agent_store import AgentRevision, AgentStore
from omnigent.stores.agent_store import events as agent_events

_T = TypeVar("_T")
NotFoundErrors = type[BaseException] | tuple[type[BaseException], ...]

DEFAULT_AGENT_HEADS_BUCKET = "OMNIGENT_AGENT_HEADS"
DEFAULT_AGENT_NAME_INDEX_BUCKET = "OMNIGENT_AGENT_NAME_INDEX"
DEFAULT_AGENT_SESSION_INDEX_BUCKET = "OMNIGENT_AGENT_SESSION_INDEX"
DEFAULT_AGENT_EVENTS_SUBJECT = "omnigent.agent_store.changed"
_CALL_TIMEOUT_S = 60.0

Connector = Callable[[], Awaitable[tuple[Any, NotFoundErrors]]]


class NatsAgentStore(AgentStore):
    """AgentStore backed by NATS JetStream KV on the consolidated NATS service."""

    def __init__(
        self,
        storage_location: str,
        *,
        connector: Connector | None = None,
        heads_bucket: str = DEFAULT_AGENT_HEADS_BUCKET,
        name_index_bucket: str = DEFAULT_AGENT_NAME_INDEX_BUCKET,
        session_index_bucket: str = DEFAULT_AGENT_SESSION_INDEX_BUCKET,
        events_subject: str = DEFAULT_AGENT_EVENTS_SUBJECT,
    ) -> None:
        super().__init__(storage_location)
        split = urlsplit(storage_location)
        self._nats_url = f"{split.scheme}://{split.netloc}" if split.scheme else storage_location
        self._heads_bucket = heads_bucket
        self._name_index_bucket = name_index_bucket
        self._session_index_bucket = session_index_bucket
        self._events_subject = events_subject
        self._connector = connector or self._default_connector

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

        self._nc: Any = None
        self._js: Any = None
        self._heads: Any = None
        self._name_index: Any = None
        self._session_index: Any = None
        self._not_found: NotFoundErrors = KeyError

    @property
    def nats_url(self) -> str:
        """The NATS server URL."""
        return self._nats_url

    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
        *,
        session_id: str | None = None,
        replace_session: bool = False,
    ) -> Automation:
        """Create a template or session-scoped agent."""
        return self._run(
            self._create(
                agent_id,
                name,
                bundle_location,
                description,
                session_id=session_id,
                replace_session=replace_session,
            )
        )

    def get(self, agent_id: str) -> Automation | None:
        return self._run(self._get(agent_id))

    def get_by_name(self, name: str) -> Automation | None:
        return self._run(self._get_by_name(name))

    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        category: str | None = None,
    ) -> PagedList[Automation]:
        return self._run(
            self._list(
                limit=limit,
                after=after,
                before=before,
                order=order,
                category=category,
            )
        )

    def get_names(self, agent_ids: list[str]) -> dict[str, str]:
        if not agent_ids:
            return {}
        return self._run(self._get_names(agent_ids))

    def update(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expected_version: int | None = None,
    ) -> Automation | None:
        return self._run(
            self._update_bundle(
                agent_id,
                bundle_location,
                expected_version=expected_version,
            )
        )

    def set_sot_tier(self, agent_id: str, tier: str | None) -> bool:
        return self._run(self._patch(agent_id, {"sot_tier": tier}, bump_version=False))

    def get_sot_tier(self, agent_id: str) -> str | None:
        agent = self.get(agent_id)
        if agent is None:
            return None
        record = self._run(self._get_record(agent_id))
        return record.get("sot_tier") if record else None

    def set_capabilities(
        self, agent_id: str, capabilities: Sequence[str] | None
    ) -> bool:
        value = [str(c) for c in capabilities] if capabilities else None
        return self._run(self._patch(agent_id, {"capabilities": value}, bump_version=False))

    def get_capabilities(self, agent_id: str) -> tuple[str, ...]:
        record = self._run(self._get_record(agent_id))
        value = record.get("capabilities") if record else None
        if not isinstance(value, list):
            return ()
        return tuple(c for c in value if isinstance(c, str))

    def set_category(self, agent_id: str, category: str | None) -> bool:
        return self._run(self._patch(agent_id, {"category": category}, bump_version=False))

    def get_category(self, agent_id: str) -> str | None:
        record = self._run(self._get_record(agent_id))
        value = record.get("category") if record else None
        return value if isinstance(value, str) else None

    def bind_session(self, agent_id: str, session_id: str) -> Automation | None:
        return self._run(self._bind_session(agent_id, session_id))

    def delete(self, agent_id: str) -> bool:
        return self._run(self._delete(agent_id))

    def list_revisions(self, agent_id: str) -> list[AgentRevision]:
        return self._run(self._history(agent_id))

    def get_revision(self, agent_id: str, revision: int) -> AgentRevision | None:
        for item in self.list_revisions(agent_id):
            if item.revision == revision:
                return item
        return None

    def diff_revisions(
        self,
        agent_id: str,
        from_revision: int,
        to_revision: int,
    ) -> dict[str, tuple[Any, Any]]:
        left = self.get_revision(agent_id, from_revision)
        right = self.get_revision(agent_id, to_revision)
        if left is None or right is None:
            return {}
        left_data = _record_from_revision(left)
        right_data = _record_from_revision(right)
        keys = sorted(set(left_data) | set(right_data))
        return {
            key: (left_data.get(key), right_data.get(key))
            for key in keys
            if left_data.get(key) != right_data.get(key)
        }

    def rollback(
        self,
        agent_id: str,
        revision: int,
        *,
        expected_version: int | None = None,
    ) -> Automation | None:
        return self._run(
            self._rollback(
                agent_id,
                revision,
                expected_version=expected_version,
            )
        )

    async def _create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None,
        *,
        session_id: str | None,
        replace_session: bool,
    ) -> Automation:
        await self._ensure_assets()
        now = _now_epoch()
        if session_id is not None and replace_session:
            old_id = await self._get_index_value(self._session_index, session_id)
            if old_id is not None and old_id != agent_id:
                await self._delete(old_id)
        record = {
            "id": agent_id,
            "created_at": now,
            "name": name,
            "bundle_location": bundle_location,
            "version": 1,
            "description": description,
            "updated_at": None,
            "session_id": session_id,
            "sot_tier": None,
            "capabilities": None,
            "category": None,
        }
        payload = _encode_record(record)
        await self._heads.create(agent_id, payload)
        try:
            if session_id is None:
                await self._name_index.create(_name_key(name), agent_id.encode("utf-8"))
            else:
                await self._session_index.create(session_id, agent_id.encode("utf-8"))
        except Exception:
            with contextlib.suppress(Exception):
                await self._heads.delete(agent_id)
            raise
        agent = _entity_from_record(record)
        await self._publish_event("created", agent_id)
        return agent

    async def _get(self, agent_id: str) -> Automation | None:
        record = await self._get_record(agent_id)
        return _entity_from_record(record) if record is not None else None

    async def _get_by_name(self, name: str) -> Automation | None:
        await self._ensure_assets()
        agent_id = await self._get_index_value(self._name_index, _name_key(name))
        if agent_id is None:
            return None
        return await self._get(agent_id)

    async def _list(
        self,
        *,
        limit: int,
        after: str | None,
        before: str | None,
        order: str,
        category: str | None,
    ) -> PagedList[Automation]:
        records = [
            record
            for record in await self._all_records()
            if record.get("session_id") is None
            and (category is None or record.get("category") == category)
        ]
        records.sort(key=lambda item: (int(item["created_at"]), str(item["id"])))
        if order == "desc":
            records.reverse()
        ids = [str(record["id"]) for record in records]
        if after is not None and after in ids:
            records = records[ids.index(after) + 1 :]
            ids = ids[ids.index(after) + 1 :]
        if before is not None and before in ids:
            records = records[: ids.index(before)]
        has_more = len(records) > limit
        records = records[:limit]
        data = [_entity_from_record(record) for record in records]
        return PagedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=has_more,
        )

    async def _get_names(self, agent_ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for agent_id in agent_ids:
            record = await self._get_record(agent_id)
            if record is not None:
                out[agent_id] = str(record["name"])
        return out

    async def _update_bundle(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expected_version: int | None,
    ) -> Automation | None:
        entry = await self._get_entry(agent_id)
        if entry is None:
            return None
        record = _decode_record(entry.value)
        current_version = int(record["version"])
        if expected_version is not None and expected_version != current_version:
            raise StaleWriteError(
                f"agent {agent_id!r} was modified concurrently "
                f"(If-Match version {expected_version} is stale)"
            )
        record["bundle_location"] = bundle_location
        record["version"] = current_version + 1
        record["updated_at"] = _now_epoch()
        await self._heads.update(agent_id, _encode_record(record), last=int(entry.revision))
        await self._publish_event("updated", agent_id)
        return _entity_from_record(record)

    async def _patch(
        self,
        agent_id: str,
        values: dict[str, Any],
        *,
        bump_version: bool,
    ) -> bool:
        entry = await self._get_entry(agent_id)
        if entry is None:
            return False
        record = _decode_record(entry.value)
        record.update(values)
        record["updated_at"] = _now_epoch()
        if bump_version:
            record["version"] = int(record["version"]) + 1
        await self._heads.update(agent_id, _encode_record(record), last=int(entry.revision))
        await self._publish_event("updated", agent_id)
        return True

    async def _bind_session(self, agent_id: str, session_id: str) -> Automation | None:
        entry = await self._get_entry(agent_id)
        if entry is None:
            return None
        record = _decode_record(entry.value)
        old_session = record.get("session_id")
        if old_session == session_id:
            return _entity_from_record(record)
        if old_session is not None:
            with contextlib.suppress(Exception):
                await self._session_index.delete(str(old_session))
        if old_session is None:
            with contextlib.suppress(Exception):
                await self._name_index.delete(_name_key(str(record["name"])))
        await self._session_index.create(session_id, agent_id.encode("utf-8"))
        record["session_id"] = session_id
        record["updated_at"] = _now_epoch()
        await self._heads.update(agent_id, _encode_record(record), last=int(entry.revision))
        await self._publish_event("updated", agent_id)
        return _entity_from_record(record)

    async def _delete(self, agent_id: str) -> bool:
        entry = await self._get_entry(agent_id)
        if entry is None:
            return False
        record = _decode_record(entry.value)
        await self._heads.delete(agent_id)
        if record.get("session_id") is None:
            with contextlib.suppress(Exception):
                await self._name_index.delete(_name_key(str(record["name"])))
        else:
            with contextlib.suppress(Exception):
                await self._session_index.delete(str(record["session_id"]))
        await self._publish_event("deleted", agent_id)
        return True

    async def _history(self, agent_id: str) -> list[AgentRevision]:
        await self._ensure_assets()
        try:
            entries = await self._heads.history(agent_id)
        except self._not_found:
            return []
        out: list[AgentRevision] = []
        for entry in entries:
            record = _decode_record(entry.value)
            out.append(
                AgentRevision(
                    revision=int(entry.revision),
                    agent=_entity_from_record(record),
                    metadata=_metadata_from_record(record),
                )
            )
        return out

    async def _rollback(
        self,
        agent_id: str,
        revision: int,
        *,
        expected_version: int | None,
    ) -> Automation | None:
        current = await self._get_entry(agent_id)
        if current is None:
            return None
        current_record = _decode_record(current.value)
        current_version = int(current_record["version"])
        if expected_version is not None and expected_version != current_version:
            raise StaleWriteError(
                f"agent {agent_id!r} was modified concurrently "
                f"(If-Match version {expected_version} is stale)"
            )
        history = await self._history(agent_id)
        target = next((item for item in history if item.revision == revision), None)
        if target is None:
            return None
        record = _record_from_revision(target)
        record["version"] = current_version + 1
        record["updated_at"] = _now_epoch()
        await self._heads.update(agent_id, _encode_record(record), last=int(current.revision))
        await self._publish_event("updated", agent_id)
        return _entity_from_record(record)

    async def _get_entry(self, agent_id: str) -> Any | None:
        await self._ensure_assets()
        try:
            return await self._heads.get(agent_id)
        except self._not_found:
            return None

    async def _get_record(self, agent_id: str) -> dict[str, Any] | None:
        entry = await self._get_entry(agent_id)
        return _decode_record(entry.value) if entry is not None else None

    async def _all_records(self) -> list[dict[str, Any]]:
        await self._ensure_assets()
        try:
            keys = await self._heads.keys()
        except self._not_found:
            return []
        records: list[dict[str, Any]] = []
        for key in keys:
            if not isinstance(key, str):
                continue
            record = await self._get_record(key)
            if record is not None:
                records.append(record)
        return records

    async def _get_index_value(self, kv: Any, key: str) -> str | None:
        try:
            entry = await kv.get(key)
        except self._not_found:
            return None
        return bytes(entry.value).decode("utf-8")

    async def _publish_event(self, action: str, agent_id: str) -> None:
        payload = json.dumps({"action": action, "agentId": agent_id}).encode("utf-8")
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.publish(self._events_subject, payload)
        agent_events.emit(action, agent_id)  # type: ignore[arg-type]

    async def _ensure_assets(self) -> None:
        if self._heads is not None:
            return
        if self._nc is None:
            self._nc, self._not_found = await self._connector()
            self._js = self._nc.jetstream()
        self._heads = await self._ensure_kv(self._heads_bucket, history=64)
        self._name_index = await self._ensure_kv(self._name_index_bucket, history=1)
        self._session_index = await self._ensure_kv(self._session_index_bucket, history=1)

    async def _ensure_kv(self, bucket: str, *, history: int) -> Any:
        try:
            return await self._js.key_value(bucket)
        except self._not_found:
            config: Any
            try:
                from nats.js.api import KeyValueConfig

                config = KeyValueConfig(bucket=bucket, history=history)
            except ImportError:
                config = {"bucket": bucket, "history": history}
            try:
                return await self._js.create_key_value(config=config)
            except TypeError:
                return await self._js.create_key_value(bucket=bucket, history=history)

    async def _default_connector(self) -> tuple[Any, NotFoundErrors]:
        try:
            import nats
            from nats.js.errors import NoKeysError, NotFoundError
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is required for the nats:// agent store; "
                "install omnigent[coordination]"
            ) from exc
        nc = await nats.connect(
            servers=[self._nats_url],
            name="omnigent-agent-store",
            max_reconnect_attempts=-1,
        )
        return nc, (NotFoundError, NoKeysError)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        with self._lifecycle_lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name=f"nats-agent-store-{id(self):x}",
                daemon=True,
            )
            thread.start()
            ready.wait()
            self._loop = loop
            self._thread = thread
            return loop

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=_CALL_TIMEOUT_S)


def _name_key(name: str) -> str:
    return name.replace("/", "%2F")


def _now_epoch() -> int:
    import time

    return int(time.time())


def _encode_record(record: dict[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_record(raw: bytes) -> dict[str, Any]:
    data = json.loads(bytes(raw).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("agent record payload must be a JSON object")
    return data


def _entity_from_record(record: dict[str, Any]) -> Automation:
    category = record.get("category") or infer_category(str(record["name"]), None)
    cls: type[Automation]
    if category == "system":
        cls = SystemAgent
    elif category == "harness":
        cls = HarnessAgent
    elif category == "workflow":
        cls = Workflow
    else:
        cls = Agent
    return cls(
        id=str(record["id"]),
        created_at=int(record["created_at"]),
        name=str(record["name"]),
        bundle_location=str(record["bundle_location"]),
        version=int(record.get("version", 1)),
        description=record.get("description"),
        updated_at=record.get("updated_at"),
        session_id=record.get("session_id"),
    )


def _record_from_entity(agent: Automation) -> dict[str, Any]:
    return {
        "id": agent.id,
        "created_at": agent.created_at,
        "name": agent.name,
        "bundle_location": agent.bundle_location,
        "version": agent.version,
        "description": agent.description,
        "updated_at": agent.updated_at,
        "session_id": agent.session_id,
        "sot_tier": None,
        "capabilities": None,
        "category": agent.category,
    }


def _metadata_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "sot_tier": record.get("sot_tier"),
        "capabilities": record.get("capabilities"),
        "category": record.get("category"),
    }


def _record_from_revision(revision: AgentRevision) -> dict[str, Any]:
    record = _record_from_entity(revision.agent)
    record.update(revision.metadata)
    return record
