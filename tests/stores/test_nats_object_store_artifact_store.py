"""Tests for NatsObjectStoreArtifactStore (BDP-2380).

The store is an **Adapter** (ADR-0008): it adapts the *async* NATS
JetStream Object Store API to the internal *sync* ``ArtifactStore``
port. All async/event-loop/thread-bridge machinery lives inside the
adapter; callers invoke it synchronously (from worker threads, via
``asyncio.to_thread``).

There is no live NATS server in CI, so these tests inject a fake
JetStream object-store backend through the adapter's connector seam.
That fake is a faithful stand-in for ``nats.js.object_store.ObjectStore``
semantics (put/get/delete + ``ObjectNotFoundError`` on a missing get),
so the adapter's real dedicated-loop / ``run_coroutine_threadsafe``
bridge is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from omnigent.stores.artifact_store.nats_object_store import (
    NatsObjectStoreArtifactStore,
)


class _FakeObjectNotFoundError(Exception):
    """Stand-in for nats.js.object_store.ObjectNotFoundError."""


class _FakeObjectStore:
    """In-memory async fake of nats.js ObjectStore for one bucket."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, name, data, meta=None):
        await asyncio.sleep(0)  # force a real await across the bridge
        if isinstance(data, str):
            data = data.encode()
        self._blobs[name] = bytes(data)

    async def get(self, name, writeinto=None, show_deleted=False):
        await asyncio.sleep(0)
        if name not in self._blobs:
            raise _FakeObjectNotFoundError(name)

        class _Result:
            def __init__(self, data: bytes) -> None:
                self.data = data

        return _Result(self._blobs[name])

    async def delete(self, name):
        await asyncio.sleep(0)
        self._blobs.pop(name, None)


class _FakeJetStream:
    """Fake JetStreamContext that hands out (and creates) object stores."""

    def __init__(self) -> None:
        self._buckets: dict[str, _FakeObjectStore] = {}

    async def object_store(self, bucket):
        if bucket not in self._buckets:
            raise _FakeObjectNotFoundError(bucket)
        return self._buckets[bucket]

    async def create_object_store(self, bucket=None, config=None, **params):
        name = bucket or (config.bucket if config else None)
        return self._buckets.setdefault(name, _FakeObjectStore())


class _FakeConnection:
    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js
        self.drained = False
        self.closed = False

    def jetstream(self):
        return self._js

    async def drain(self) -> None:
        self.drained = True

    async def close(self) -> None:
        self.closed = True


def _make_store(bucket: str = "omnigent-artifacts") -> NatsObjectStoreArtifactStore:
    """Build an adapter wired to a fresh fake NATS connection.

    The connector returns a fake connection + the ObjectNotFoundError
    type the adapter must map to ``KeyError``.
    """
    fake_conn = _FakeConnection(_FakeJetStream())

    async def _connector():
        return fake_conn, _FakeObjectNotFoundError

    return NatsObjectStoreArtifactStore(
        f"nats://localhost:4222/{bucket}", connector=_connector
    )


# ── put / get round-trip ────────────────────────────────────


def test_put_and_get() -> None:
    store = _make_store()
    store.put("abc123", b"hello world")
    assert store.get("abc123") == b"hello world"


def test_put_overwrites() -> None:
    store = _make_store()
    store.put("k", b"first")
    store.put("k", b"second")
    assert store.get("k") == b"second"


def test_content_addressed_key_with_slash() -> None:
    """Content-addressed keys ({agent_id}/{sha256}) contain a slash."""
    store = _make_store()
    key = "ag_abc123/" + "d" * 64
    store.put(key, b"bundle-bytes")
    assert store.exists(key)
    assert store.get(key) == b"bundle-bytes"


# ── exists ──────────────────────────────────────────────────


def test_exists_true_false() -> None:
    store = _make_store()
    assert not store.exists("absent")
    store.put("present", b"x")
    assert store.exists("present")


# ── get errors ──────────────────────────────────────────────


def test_get_missing_raises_key_error() -> None:
    store = _make_store()
    with pytest.raises(KeyError, match=r"no-such-key"):
        store.get("no-such-key")


# ── delete ──────────────────────────────────────────────────


def test_delete_removes_blob() -> None:
    store = _make_store()
    store.put("to-delete", b"data")
    store.delete("to-delete")
    assert not store.exists("to-delete")


def test_delete_missing_is_noop() -> None:
    store = _make_store()
    store.delete("nonexistent")  # must not raise


# ── large blob (Object Store chunking) ──────────────────────


def test_multi_megabyte_blob_round_trips() -> None:
    store = _make_store()
    blob = bytes(range(256)) * (5 * 4096)  # ~5 MB, > default chunk size
    store.put("ag_big/" + "a" * 64, blob)
    assert store.get("ag_big/" + "a" * 64) == blob


# ── async/sync bridge contract ──────────────────────────────


def test_runs_on_a_dedicated_background_loop_not_the_caller() -> None:
    """The adapter must own its own loop on a background thread.

    A caller invoking the sync API from a worker thread (the real
    invocation pattern: ``asyncio.to_thread``) must not need — or
    collide with — a running event loop on that thread.
    """
    store = _make_store()
    result: dict[str, object] = {}

    def worker() -> None:
        # No event loop on this thread; the adapter must supply its own.
        store.put("from-thread", b"payload")
        result["value"] = store.get("from-thread")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert result["value"] == b"payload"


def test_connection_is_reused_across_calls() -> None:
    """Lazy connect once; reuse the connection for subsequent calls."""
    connects = {"n": 0}
    fake_conn = _FakeConnection(_FakeJetStream())

    async def _connector():
        connects["n"] += 1
        return fake_conn, _FakeObjectNotFoundError

    store = NatsObjectStoreArtifactStore(
        "nats://localhost:4222/omnigent-artifacts", connector=_connector
    )
    store.put("a", b"1")
    store.put("b", b"2")
    store.get("a")
    assert connects["n"] == 1


# ── fail-loud on unreachable NATS ───────────────────────────


def test_unreachable_nats_raises_not_empty_store() -> None:
    """A connection failure must surface loudly, never as an empty store."""

    async def _connector():
        raise ConnectionError("nats unreachable")

    store = NatsObjectStoreArtifactStore(
        "nats://localhost:4222/omnigent-artifacts", connector=_connector
    )
    with pytest.raises(Exception, match=r"nats unreachable|unreachable"):
        store.get("anything")


# ── URL parsing: server + bucket ────────────────────────────


def test_default_bucket_when_url_has_no_path() -> None:
    store = _make_store()  # uses explicit bucket via _make_store
    # A bare nats://host:port URL (no /bucket) falls back to the default.
    bare = NatsObjectStoreArtifactStore("nats://localhost:4222")
    assert bare.bucket == "omnigent-artifacts"
    assert store.bucket == "omnigent-artifacts"


def test_bucket_parsed_from_url_path() -> None:
    store = NatsObjectStoreArtifactStore("nats://localhost:4222/my-bucket")
    assert store.bucket == "my-bucket"
    assert store.nats_url == "nats://localhost:4222"
